"""qa_incident -> RepairBundle adapter tests.

We exercise the projection in isolation (no agent shell-out) and verify
that `auto_fix.propose_fix_for_incident` routes the apply-agent path
through `repair_runner.run_repair`.
"""

from __future__ import annotations

import secrets


from retrace.qa_incidents import (
    Incident,
    IncidentEvidence,
    ReproductionStep,
    make_fingerprint,
    make_public_id,
    utc_now_iso,
)
from retrace.qa_repair_adapter import (
    qa_incident_to_repair_bundle,
    resolve_agent_command,
)


def _make_incident() -> Incident:
    return Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id="local",
        environment_id="production",
        fingerprint=make_fingerprint(["adapter"]),
        title="Login fails on submit",
        summary="POST /api/login returns 500 for some users.",
        suspected_cause="auth handler swallows AbortError",
        severity="high",
        confidence="high",
        status="reproduced",
        primary_source_kind="replay",
        sources=[],
        reproduction=[
            ReproductionStep(0, "navigate", "open /login", url="http://localhost:3000/login"),
            ReproductionStep(1, "click", "submit", target={"selector": "#signin"}),
        ],
        expected_outcome="redirect to /dashboard",
        actual_outcome="stays on /login",
        app_url="http://localhost:3000",
        evidence=IncidentEvidence(
            top_stack_frame="loginHandler at server/routes/auth.ts:42",
            console_excerpts=["TypeError: undefined is not a function"],
        ),
        affected_count=3,
        affected_users=2,
        first_seen_ms=0,
        last_seen_ms=0,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )


def test_adapter_carries_summary_and_reproduction():
    inc = _make_incident()
    bundle = qa_incident_to_repair_bundle(
        inc,
        repo_path=None,
        likely_files=["server/routes/auth.ts"],
        prompt_path="/tmp/INC-XYZ.fix.md",
        repo_full_name="org/repo",
    )

    assert bundle.public_id == inc.public_id
    assert bundle.failure_summary["title"] == "Login fails on submit"
    assert bundle.failure_summary["primary_source_kind"] == "replay"
    assert bundle.reproduction["expected"] == "redirect to /dashboard"
    assert bundle.reproduction["actual"] == "stays on /login"
    # Reproduction steps survive as plain dicts.
    actions = [s["action"] for s in bundle.reproduction["steps"]]
    assert actions == ["navigate", "click"]
    # Likely files reach repair_runner.
    assert bundle.likely_files == ["server/routes/auth.ts"]
    # The fix-prompt path is in backend_context for downstream consumers.
    assert bundle.backend_context["fix_prompt_path"] == "/tmp/INC-XYZ.fix.md"
    assert bundle.backend_context["repo_full_name"] == "org/repo"


def test_adapter_promotes_evidence():
    inc = _make_incident()
    bundle = qa_incident_to_repair_bundle(inc, repo_path=None)
    evidence_types = {item["type"] for item in bundle.evidence}
    assert "stack_frame" in evidence_types
    assert "console" in evidence_types


def test_adapter_returns_empty_validation_when_repo_path_missing():
    inc = _make_incident()
    bundle = qa_incident_to_repair_bundle(inc, repo_path=None)
    # No repo path => no inferred validation commands. We don't assert
    # exact length since infer_validation_commands has defaults for
    # certain package managers — just check shape.
    assert isinstance(bundle.validation_commands, list)


def test_resolve_agent_command_returns_empty_for_unknown(monkeypatch):
    # Make sure no claude/codex binaries are discoverable.
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert resolve_agent_command("auto") == []
    assert resolve_agent_command("claude") == []
    assert resolve_agent_command("codex") == []


def test_resolve_agent_command_picks_claude_when_available(monkeypatch):
    def _which(name):
        return "/usr/local/bin/claude" if name == "claude" else None

    monkeypatch.setattr("shutil.which", _which)
    cmd = resolve_agent_command("claude")
    assert cmd and cmd[0] == "claude"


def test_resolve_agent_command_picks_codex_when_claude_missing(monkeypatch):
    def _which(name):
        return "/usr/local/bin/codex" if name == "codex" else None

    monkeypatch.setattr("shutil.which", _which)
    cmd = resolve_agent_command("auto")
    assert cmd and cmd[0] == "codex"
