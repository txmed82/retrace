"""Unified Incident model.

An Incident is the single canonical object across the three Retrace surfaces:

  - Replay-derived findings (user error in production)
  - UI test failures (AI-driven browser tests)
  - API test failures (backend / contract tests)

Every detector, every test run, every error monitor signal converges on this
shape. Downstream consumers (UI, MCP, the auto-repro + auto-fix pipeline) only
need to understand Incident — not the dozen tables that contributed to it.

The Incident also carries the *operational* state of the killer-demo flow:

  user bug  ->  auto-generated test  ->  AI fix PR

so callers can ask a single object: "has it been reproduced? has a fix PR been
opened? what's the next action?"
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Enums (kept as plain strings so SQLite migrations stay trivial).
# ---------------------------------------------------------------------------

SOURCE_KINDS = (
    "replay",        # rrweb session signal / cluster
    "ui_test",       # tester run failure
    "api_test",      # backend / contract test failure
    "error_monitor", # frontend/backend error event (sentry-style)
    "manual",        # human-filed
)

SEVERITIES = ("low", "medium", "high", "critical")

INCIDENT_STATUSES = (
    "open",          # newly opened, no work yet
    "reproducing",   # auto-repro in flight
    "reproduced",    # repro test confirmed the bug
    "not_reproduced",
    "fixing",        # fix prompt sent, agent working
    "fix_proposed",  # PR opened, awaiting review
    "resolved",
    "ignored",
)

REPRO_STATUSES = ("not_attempted", "running", "confirmed", "not_confirmed", "error")

FIX_STATUSES = ("not_started", "prompt_ready", "applied", "pr_open", "merged", "error")


# ---------------------------------------------------------------------------
# Data shape.
# ---------------------------------------------------------------------------


@dataclass
class ReproductionStep:
    """A single step in the canonical reproduction recipe."""

    index: int
    action: str                  # "navigate" | "click" | "input" | "wait" | "assert"
    description: str             # human-readable
    target: dict[str, Any] = field(default_factory=dict)  # selector/aria/text
    value: str = ""              # input value (or "<masked>")
    url: str = ""
    timestamp_ms: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ReproductionStep":
        return ReproductionStep(
            index=int(d.get("index", 0) or 0),
            action=str(d.get("action", "")),
            description=str(d.get("description", "")),
            target=dict(d.get("target", {}) or {}),
            value=str(d.get("value", "")),
            url=str(d.get("url", "")),
            timestamp_ms=int(d.get("timestamp_ms", 0) or 0),
        )


@dataclass
class IncidentEvidence:
    """Pointers from an Incident back to the underlying raw data.

    Stored as a JSON blob on the Incident row. We only keep ids and links so
    the Incident row stays small — fetch the heavy stuff from the source
    tables when needed.
    """

    replay_session_ids: list[str] = field(default_factory=list)
    replay_issue_ids: list[str] = field(default_factory=list)
    report_finding_ids: list[int] = field(default_factory=list)
    tester_run_ids: list[str] = field(default_factory=list)
    api_test_run_ids: list[str] = field(default_factory=list)
    error_issue_ids: list[str] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    console_excerpts: list[str] = field(default_factory=list)
    network_failures: list[dict[str, Any]] = field(default_factory=list)
    primary_url: str = ""
    replay_url: str = ""
    top_stack_frame: str = ""


@dataclass
class IncidentSource:
    """A single contributing piece of evidence to an Incident."""

    kind: str                   # one of SOURCE_KINDS
    ref_id: str                 # foreign id into the source table (string for flexibility)
    score: float = 1.0          # cluster-weight contribution
    note: str = ""              # short human explanation
    created_at: str = ""


@dataclass
class Incident:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    fingerprint: str

    title: str
    summary: str
    suspected_cause: str
    severity: str
    confidence: str
    status: str

    primary_source_kind: str
    sources: list[IncidentSource]

    reproduction: list[ReproductionStep]
    expected_outcome: str
    actual_outcome: str
    app_url: str

    evidence: IncidentEvidence

    affected_count: int
    affected_users: int
    first_seen_ms: int
    last_seen_ms: int

    repro_status: str = "not_attempted"
    repro_spec_id: str = ""
    repro_run_id: str = ""
    repro_summary: str = ""

    fix_status: str = "not_started"
    fix_repo: str = ""
    fix_branch: str = ""
    fix_pr_url: str = ""
    fix_prompt_path: str = ""

    created_at: str = ""
    updated_at: str = ""

    # ----- serialization helpers -----

    def to_row(self) -> dict[str, Any]:
        """Flatten to the column-shape the `incidents` table expects."""
        return {
            "id": self.id,
            "public_id": self.public_id,
            "project_id": self.project_id,
            "environment_id": self.environment_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "summary": self.summary,
            "suspected_cause": self.suspected_cause,
            "severity": self.severity,
            "confidence": self.confidence,
            "status": self.status,
            "primary_source_kind": self.primary_source_kind,
            "sources_json": json.dumps([asdict(s) for s in self.sources]),
            "reproduction_json": json.dumps([asdict(s) for s in self.reproduction]),
            "expected_outcome": self.expected_outcome,
            "actual_outcome": self.actual_outcome,
            "app_url": self.app_url,
            "evidence_json": json.dumps(asdict(self.evidence)),
            "affected_count": int(self.affected_count),
            "affected_users": int(self.affected_users),
            "first_seen_ms": int(self.first_seen_ms),
            "last_seen_ms": int(self.last_seen_ms),
            "repro_status": self.repro_status,
            "repro_spec_id": self.repro_spec_id,
            "repro_run_id": self.repro_run_id,
            "repro_summary": self.repro_summary,
            "fix_status": self.fix_status,
            "fix_repo": self.fix_repo,
            "fix_branch": self.fix_branch,
            "fix_pr_url": self.fix_pr_url,
            "fix_prompt_path": self.fix_prompt_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_row(row: Any) -> "Incident":
        def _g(name: str, default: Any = "") -> Any:
            try:
                return row[name]
            except (KeyError, IndexError):
                return default

        def _safe_json(raw: Any, fallback: Any) -> Any:
            """Decode a JSON column; never raise for one malformed row.

            A single bad `evidence_json` (e.g. truncated mid-write or hand-
            edited in sqlite) used to crash the whole `qa list` / `qa auto`
            pipeline. We swallow the parse error, log it, and substitute
            the typed default so other incidents in the same query still
            render. If the parsed value isn't the expected container type
            we also fall back, so downstream `.get(...)` is safe.
            """
            if raw is None or raw == "":
                return fallback
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                logger = __import__("logging").getLogger(__name__)
                logger.warning(
                    "qa_incidents: malformed JSON column for incident, falling back: %s",
                    exc,
                )
                return fallback
            if not isinstance(parsed, type(fallback)):
                return fallback
            return parsed

        sources_raw = _safe_json(_g("sources_json", "[]"), [])
        repro_raw = _safe_json(_g("reproduction_json", "[]"), [])
        evidence_raw = _safe_json(_g("evidence_json", "{}"), {})

        return Incident(
            id=str(_g("id", "")),
            public_id=str(_g("public_id", "")),
            project_id=str(_g("project_id", "")),
            environment_id=str(_g("environment_id", "")),
            fingerprint=str(_g("fingerprint", "")),
            title=str(_g("title", "")),
            summary=str(_g("summary", "")),
            suspected_cause=str(_g("suspected_cause", "")),
            severity=str(_g("severity", "medium")),
            confidence=str(_g("confidence", "medium")),
            status=str(_g("status", "open")),
            primary_source_kind=str(_g("primary_source_kind", "replay")),
            sources=[
                IncidentSource(
                    kind=str(s.get("kind", "")),
                    ref_id=str(s.get("ref_id", "")),
                    score=float(s.get("score", 1.0) or 0.0),
                    note=str(s.get("note", "")),
                    created_at=str(s.get("created_at", "")),
                )
                for s in sources_raw
                if isinstance(s, dict)
            ],
            reproduction=[
                ReproductionStep.from_dict(s)
                for s in repro_raw
                if isinstance(s, dict)
            ],
            expected_outcome=str(_g("expected_outcome", "")),
            actual_outcome=str(_g("actual_outcome", "")),
            app_url=str(_g("app_url", "")),
            evidence=IncidentEvidence(
                replay_session_ids=list(evidence_raw.get("replay_session_ids", []) or []),
                replay_issue_ids=list(evidence_raw.get("replay_issue_ids", []) or []),
                report_finding_ids=list(evidence_raw.get("report_finding_ids", []) or []),
                tester_run_ids=list(evidence_raw.get("tester_run_ids", []) or []),
                api_test_run_ids=list(evidence_raw.get("api_test_run_ids", []) or []),
                error_issue_ids=list(evidence_raw.get("error_issue_ids", []) or []),
                trace_ids=list(evidence_raw.get("trace_ids", []) or []),
                console_excerpts=list(evidence_raw.get("console_excerpts", []) or []),
                network_failures=list(evidence_raw.get("network_failures", []) or []),
                primary_url=str(evidence_raw.get("primary_url", "")),
                replay_url=str(evidence_raw.get("replay_url", "")),
                top_stack_frame=str(evidence_raw.get("top_stack_frame", "")),
            ),
            affected_count=int(_g("affected_count", 1) or 1),
            affected_users=int(_g("affected_users", 1) or 1),
            first_seen_ms=int(_g("first_seen_ms", 0) or 0),
            last_seen_ms=int(_g("last_seen_ms", 0) or 0),
            repro_status=str(_g("repro_status", "not_attempted")),
            repro_spec_id=str(_g("repro_spec_id", "")),
            repro_run_id=str(_g("repro_run_id", "")),
            repro_summary=str(_g("repro_summary", "")),
            fix_status=str(_g("fix_status", "not_started")),
            fix_repo=str(_g("fix_repo", "")),
            fix_branch=str(_g("fix_branch", "")),
            fix_pr_url=str(_g("fix_pr_url", "")),
            fix_prompt_path=str(_g("fix_prompt_path", "")),
            created_at=str(_g("created_at", "")),
            updated_at=str(_g("updated_at", "")),
        )


# ---------------------------------------------------------------------------
# Public-id / fingerprint helpers.
# ---------------------------------------------------------------------------


_PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L for legibility


def make_public_id() -> str:
    """Short shareable id, e.g., `INC-AB12CD`."""
    return "INC-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(6))


def make_fingerprint(parts: Iterable[str]) -> str:
    """Deterministic clustering fingerprint."""
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").strip().lower().encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Sensitive-data redaction.
#
# Incidents are persisted to SQLite and later embedded in fix prompts that
# may be checked into a git branch and surfaced inside a PR. Anything raw
# we capture from production-shaped payloads (API request bodies, response
# bodies, custom headers) can leak credentials and PII downstream. The
# redactor below is intentionally conservative: it lower-cases known secret-
# key patterns (`api_key`, `password`, `authorization`, ...), masks long
# bearer-looking tokens, and trims to the size budget caller asked for.
# ---------------------------------------------------------------------------


_SECRET_KEY_RE = __import__("re").compile(
    r'(?P<key>"(?:api[_-]?key|secret|password|token|authorization|bearer|access[_-]?token|refresh[_-]?token|client[_-]?secret|cookie|set-cookie|x-api-key)"\s*:\s*)"(?P<val>[^"\\]{0,2048}(?:\\.[^"\\]{0,2048}){0,8})"',
    __import__("re").IGNORECASE,
)
_BEARER_RE = __import__("re").compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{8,})")
_LONG_TOKEN_RE = __import__("re").compile(r"\b([A-Za-z0-9._\-]{32,})\b")
_EMAIL_RE = __import__("re").compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_sensitive_text(text: str, *, max_len: int = 2048) -> str:
    """Best-effort redaction of secrets/PII from free-form payload strings.

    Designed for the API-test ingest path where request/response bodies are
    captured as opaque strings. We don't try to parse JSON (payloads may be
    truncated or non-JSON) — we just substitute the common shapes that
    would otherwise leak into stored incidents and fix prompts.
    """
    if not text:
        return ""
    redacted = _SECRET_KEY_RE.sub(lambda m: f'{m.group("key")}"<redacted>"', text)
    redacted = _BEARER_RE.sub(lambda m: f"{m.group(1)}<redacted>", redacted)
    redacted = _LONG_TOKEN_RE.sub(
        lambda m: "<redacted>" if "_" in m.group(1) or "-" in m.group(1) else m.group(1),
        redacted,
    )
    redacted = _EMAIL_RE.sub("<redacted-email>", redacted)
    if len(redacted) > max_len:
        return redacted[: max_len - 16] + "…<truncated>"
    return redacted


# ---------------------------------------------------------------------------
# Adapters: existing record shapes -> Incident.
# ---------------------------------------------------------------------------


def incident_from_replay_issue(
    issue_row: Any,
    *,
    sessions: Optional[list[Any]] = None,
    representative_session_id: str = "",
    replay_url_builder: Optional[Any] = None,
) -> Incident:
    """Build an Incident from a `replay_issues` row.

    `replay_issues` is already the closest thing we have to an Incident.
    This adapter normalizes its shape into the canonical model so the rest of
    the system stops branching on "is it a replay_issue or a finding?".
    """

    def _g(name: str, default: Any = "") -> Any:
        try:
            return issue_row[name]
        except (KeyError, IndexError):
            return default

    public_id = str(_g("public_id") or make_public_id())
    fingerprint = str(_g("fingerprint") or make_fingerprint([public_id]))
    rep_session = str(_g("representative_session_id") or representative_session_id)
    session_ids = []
    if rep_session:
        session_ids.append(rep_session)
    for s in sessions or []:
        try:
            sid = str(s["session_id"])
            if sid and sid not in session_ids:
                session_ids.append(sid)
        except (KeyError, IndexError, TypeError):
            continue

    repro_raw = []
    try:
        repro_raw = json.loads(_g("reproduction_steps_json", "[]") or "[]") or []
    except Exception:
        repro_raw = []

    repro_steps: list[ReproductionStep] = []
    for i, step in enumerate(repro_raw):
        if isinstance(step, str):
            repro_steps.append(
                ReproductionStep(index=i, action="describe", description=step)
            )
        elif isinstance(step, dict):
            d = dict(step)
            d.setdefault("index", i)
            repro_steps.append(ReproductionStep.from_dict(d))

    evidence_raw: dict[str, Any] = {}
    try:
        evidence_raw = json.loads(_g("evidence_json", "{}") or "{}") or {}
    except Exception:
        evidence_raw = {}

    replay_url = ""
    if replay_url_builder and rep_session:
        try:
            replay_url = replay_url_builder(rep_session)
        except Exception:
            replay_url = ""

    primary_url = str(evidence_raw.get("primary_url", "") or "")

    evidence = IncidentEvidence(
        replay_session_ids=session_ids,
        replay_issue_ids=[str(_g("id", ""))],
        primary_url=primary_url,
        replay_url=replay_url,
        top_stack_frame=str(evidence_raw.get("top_stack_frame", "") or ""),
        console_excerpts=list(evidence_raw.get("console_excerpts", []) or []),
        network_failures=list(evidence_raw.get("network_failures", []) or []),
        error_issue_ids=list(evidence_raw.get("error_issue_ids", []) or []),
        trace_ids=list(evidence_raw.get("trace_ids", []) or []),
    )

    now = utc_now_iso()
    return Incident(
        id=str(_g("id", "")) or secrets.token_hex(16),
        public_id=public_id,
        project_id=str(_g("project_id", "")),
        environment_id=str(_g("environment_id", "")),
        fingerprint=fingerprint,
        title=str(_g("title", "") or "Replay incident"),
        summary=str(_g("summary", "") or ""),
        suspected_cause=str(_g("likely_cause", "") or ""),
        severity=str(_g("severity", "medium") or "medium"),
        confidence=str(_g("confidence", "medium") or "medium"),
        status="open",
        primary_source_kind="replay",
        sources=[
            IncidentSource(
                kind="replay",
                ref_id=str(_g("id", "")),
                score=1.0,
                note="replay issue",
                created_at=now,
            )
        ],
        reproduction=repro_steps,
        expected_outcome="",
        actual_outcome=str(_g("summary", "") or ""),
        app_url=primary_url,
        evidence=evidence,
        affected_count=int(_g("affected_count", 1) or 1),
        affected_users=int(_g("affected_users", 1) or 1),
        first_seen_ms=int(_g("first_seen_ms", 0) or 0),
        last_seen_ms=int(_g("last_seen_ms", 0) or 0),
        created_at=now,
        updated_at=now,
    )


def incident_from_finding(
    finding: Any,
    *,
    project_id: str = "local",
    environment_id: str = "production",
    app_url: str = "",
) -> Incident:
    """Build an Incident from a `sinks.base.Finding` (PostHog pipeline)."""

    repro_steps: list[ReproductionStep] = []
    for i, step in enumerate(getattr(finding, "reproduction_steps", []) or []):
        if isinstance(step, str):
            repro_steps.append(
                ReproductionStep(index=i, action="describe", description=step)
            )
        elif isinstance(step, dict):
            d = dict(step)
            d.setdefault("index", i)
            repro_steps.append(ReproductionStep.from_dict(d))

    session_id = getattr(finding, "session_id", "") or ""
    session_url = getattr(finding, "session_url", "") or ""
    evidence = IncidentEvidence(
        replay_session_ids=[session_id] if session_id else [],
        replay_issue_ids=[],
        primary_url=app_url or "",
        replay_url=session_url,
        top_stack_frame=getattr(finding, "top_stack_frame", "") or "",
        error_issue_ids=list(getattr(finding, "error_issue_ids", []) or []),
        trace_ids=list(getattr(finding, "trace_ids", []) or []),
    )

    fp = make_fingerprint(
        [
            getattr(finding, "title", "") or "",
            getattr(finding, "category", "") or "",
            evidence.top_stack_frame,
        ]
    )
    now = utc_now_iso()
    return Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id=project_id,
        environment_id=environment_id,
        fingerprint=fp,
        title=str(getattr(finding, "title", "Replay incident") or "Replay incident"),
        summary=str(getattr(finding, "what_happened", "") or ""),
        suspected_cause=str(getattr(finding, "likely_cause", "") or ""),
        severity=str(getattr(finding, "severity", "medium") or "medium"),
        confidence=str(getattr(finding, "confidence", "medium") or "medium"),
        status="open",
        primary_source_kind="replay",
        sources=[
            IncidentSource(
                kind="replay",
                ref_id=session_id,
                score=1.0,
                note="posthog replay finding",
                created_at=now,
            )
        ],
        reproduction=repro_steps,
        expected_outcome="",
        actual_outcome=str(getattr(finding, "what_happened", "") or ""),
        app_url=app_url or "",
        evidence=evidence,
        affected_count=int(getattr(finding, "affected_count", 1) or 1),
        affected_users=int(getattr(finding, "affected_count", 1) or 1),
        first_seen_ms=int(getattr(finding, "first_seen_ms", 0) or 0),
        last_seen_ms=int(getattr(finding, "last_seen_ms", 0) or 0),
        created_at=now,
        updated_at=now,
    )


def incident_from_tester_run(
    run_result: Any,
    spec: Any,
    *,
    project_id: str = "local",
    environment_id: str = "production",
) -> Incident:
    """Build an Incident from a failed `TesterRunResult`."""

    now = utc_now_iso()
    # `spec` is typed as `Any` so the fallback must also use `getattr`;
    # otherwise the inner `spec.spec_id` evaluates eagerly and explodes on
    # specs that lack one (some tests pass minimal stubs).
    spec_name = getattr(spec, "name", None) or getattr(spec, "spec_id", "unknown-spec")
    title = f"UI test failed: {spec_name}"
    summary = str(getattr(run_result, "error", "") or "UI test produced a failing assertion.")
    failed = [
        a for a in getattr(run_result, "assertion_results", []) or []
        if isinstance(a, dict) and not a.get("ok", True)
    ]
    if failed:
        first = failed[0]
        title = f"UI test failed: {first.get('assertion_type', 'assertion')}"
        summary = str(first.get("message", "") or summary)

    repro_steps: list[ReproductionStep] = []
    for i, step in enumerate(getattr(spec, "exact_steps", []) or []):
        if isinstance(step, dict):
            d = dict(step)
            d.setdefault("index", i)
            d.setdefault("action", step.get("action") or "describe")
            d.setdefault("description", step.get("description") or json.dumps(step)[:120])
            repro_steps.append(ReproductionStep.from_dict(d))
    if not repro_steps and getattr(spec, "prompt", ""):
        repro_steps.append(
            ReproductionStep(index=0, action="describe", description=str(spec.prompt))
        )

    fp = make_fingerprint([title, getattr(spec, "spec_id", "") or "", getattr(spec, "app_url", "") or ""])
    return Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id=project_id,
        environment_id=environment_id,
        fingerprint=fp,
        title=title,
        summary=summary,
        suspected_cause="",
        severity="medium",
        confidence="high",
        status="reproduced",
        primary_source_kind="ui_test",
        sources=[
            IncidentSource(
                kind="ui_test",
                ref_id=getattr(run_result, "run_id", "") or "",
                score=1.0,
                note=f"tester run {getattr(run_result, 'status', '')}",
                created_at=now,
            )
        ],
        reproduction=repro_steps,
        expected_outcome="UI test pass",
        actual_outcome=summary,
        app_url=str(getattr(spec, "app_url", "") or ""),
        evidence=IncidentEvidence(
            tester_run_ids=[getattr(run_result, "run_id", "") or ""],
            primary_url=str(getattr(spec, "app_url", "") or ""),
        ),
        affected_count=1,
        affected_users=1,
        first_seen_ms=0,
        last_seen_ms=0,
        repro_status="confirmed",
        repro_spec_id=getattr(spec, "spec_id", "") or "",
        repro_run_id=getattr(run_result, "run_id", "") or "",
        repro_summary=summary,
        created_at=now,
        updated_at=now,
    )


def incident_from_api_test(
    *,
    project_id: str,
    environment_id: str,
    title: str,
    summary: str,
    method: str,
    url: str,
    expected_status: int,
    actual_status: int,
    request_body: str = "",
    response_body: str = "",
    suspected_cause: str = "",
    run_id: str = "",
) -> Incident:
    """Build an Incident from an API test failure."""

    now = utc_now_iso()
    # Captured payloads can carry credentials and PII. Always redact before
    # we persist or echo them through fix prompts.
    safe_request_body = redact_sensitive_text(request_body or "", max_len=2048)
    safe_response_body = redact_sensitive_text(response_body or "", max_len=2048)
    fp = make_fingerprint([title, method, url, str(expected_status), str(actual_status)])
    repro = [
        ReproductionStep(
            index=0,
            action="api_call",
            description=f"{method.upper()} {url}",
            target={"method": method.upper(), "url": url},
            value=safe_request_body,
            url=url,
        ),
        ReproductionStep(
            index=1,
            action="assert",
            description=f"expected HTTP {expected_status}, got {actual_status}",
            target={"expected_status": expected_status, "actual_status": actual_status},
        ),
    ]
    return Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id=project_id,
        environment_id=environment_id,
        fingerprint=fp,
        title=title,
        summary=summary,
        suspected_cause=suspected_cause,
        severity="high" if actual_status >= 500 else "medium",
        confidence="high",
        status="reproduced",
        primary_source_kind="api_test",
        sources=[
            IncidentSource(
                kind="api_test",
                ref_id=run_id or "",
                score=1.0,
                note=f"{method.upper()} {url} -> {actual_status}",
                created_at=now,
            )
        ],
        reproduction=repro,
        expected_outcome=f"HTTP {expected_status}",
        actual_outcome=f"HTTP {actual_status}: {safe_response_body[:200]}",
        app_url=url,
        evidence=IncidentEvidence(
            api_test_run_ids=[run_id] if run_id else [],
            primary_url=url,
        ),
        affected_count=1,
        affected_users=0,
        first_seen_ms=0,
        last_seen_ms=0,
        repro_status="confirmed",
        repro_summary=summary,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Convenience: derive a TesterSpec prompt from an incident's reproduction.
# Kept here (not in tester.py) so the dependency direction stays Incidents
# -> tester, not tester -> incidents.
# ---------------------------------------------------------------------------


def reproduction_prompt_for_incident(inc: Incident) -> str:
    """Render a natural-language prompt usable by Browser Harness / a UI tester.

    Output is intentionally short and instruction-shaped so a small model
    can follow it.
    """
    lines: list[str] = []
    lines.append(f"Goal: reproduce the bug described as: {inc.title}.")
    if inc.summary:
        lines.append(f"What users see: {inc.summary}")
    if inc.app_url:
        lines.append(f"Start at: {inc.app_url}")
    if inc.reproduction:
        lines.append("Steps:")
        for s in inc.reproduction:
            label = s.description or s.action
            if s.target:
                label += f" (target: {json.dumps(s.target, separators=(',', ':'))[:160]})"
            if s.value and s.value != "<masked>":
                label += f" with value `{s.value[:80]}`"
            lines.append(f"  {s.index + 1}. {label}")
    if inc.expected_outcome:
        lines.append(f"Expected: {inc.expected_outcome}")
    if inc.actual_outcome:
        lines.append(f"Actual (the bug): {inc.actual_outcome}")
    lines.append("Pass = you reproduced the failure. Fail = you couldn't trigger it.")
    return "\n".join(lines)
