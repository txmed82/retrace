"""Bridge: master's `failures` + `incidents` + `replay_issues` -> `qa_incidents`.

Retrace has two incident concepts that until now didn't talk to each other:

  - The **monitoring/repair side** (`failures`, `incidents`, `evidence`,
    `repair_tasks`) — populated by Sentry compat ingest, OTel logs,
    monitoring webhooks, API test failures, deploy correlation.
  - The **QA pipeline side** (`qa_incidents`) — populated by `retrace qa
    reproduce|fix|auto` to drive the replay → test → fix-PR loop.

This module is the single converter that keeps the QA pipeline aware of
every signal class. Call `sync_qa_incident_from_failure(...)` whenever a
`FailureRow` is upserted; call `sync_qa_incident_from_replay_issue(...)`
whenever a replay-backed issue lands. After this the `qa list` / `qa
auto` surface really does reflect "every signal across the product".
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Iterable, Optional

from retrace.qa_incidents import (
    Incident,
    IncidentEvidence,
    IncidentSource,
    ReproductionStep,
    incident_from_replay_issue,
    make_fingerprint,
    make_public_id,
    utc_now_iso,
)
from retrace.storage import FailureRow, Storage


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def sync_qa_incident_from_failure(
    *,
    store: Storage,
    failure_id: str,
) -> Optional[str]:
    """Convert a `FailureRow` into a `qa_incident` upsert.

    Returns the QA incident's public id on success, or None when the
    failure is missing or the conversion fails. Bridge errors must never
    crash the producer — monitoring ingest happens on hot paths.
    """
    try:
        failure = store.get_failure_by_id(failure_id)
        if failure is None:
            return None
        incident = _incident_from_failure(failure)
        # Use the variant that returns the persisted public_id: on a
        # fingerprint collision the existing row keeps its original
        # public_id, so the candidate value in `incident.to_row()` is
        # dropped. Return the canonical one so callers don't get dead
        # references on resync.
        _id, public_id, _inserted = store.upsert_qa_incident_returning(incident.to_row())
        return public_id
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "qa_incident_bridge: failed to sync failure %s: %s",
            failure_id,
            exc,
        )
        return None


def sync_qa_incidents_from_failures(
    *,
    store: Storage,
    failure_ids: Iterable[str],
) -> list[str]:
    """Batch helper. Skips conversions that fail."""
    out: list[str] = []
    for fid in failure_ids:
        pub = sync_qa_incident_from_failure(store=store, failure_id=fid)
        if pub:
            out.append(pub)
    return out


def sync_qa_incident_from_replay_issue(
    *,
    store: Storage,
    issue_row: Any,
    representative_session_id: str = "",
    replay_url_builder: Any = None,
) -> Optional[str]:
    """Convert a `replay_issues` row into a `qa_incident`.

    Uses the existing `incident_from_replay_issue` adapter and the
    incident's session-list helper, then upserts. Same error-swallowing
    contract as the failure path.
    """
    try:
        sessions: list[Any] = []
        try:
            sessions = store.list_replay_issue_sessions(str(issue_row["id"]))
        except Exception:
            sessions = []
        incident = incident_from_replay_issue(
            issue_row,
            sessions=sessions,
            representative_session_id=representative_session_id,
            replay_url_builder=replay_url_builder,
        )
        _id, public_id, _inserted = store.upsert_qa_incident_returning(incident.to_row())
        return public_id
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("qa_incident_bridge: failed to sync replay issue: %s", exc)
        return None


def sync_qa_incident_from_pr_review_finding(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    title: str,
    summary: str,
    repo: str,
    pr_number: int,
    files: list[str],
    suspected_cause: str = "",
    severity: str = "medium",
) -> Optional[str]:
    """File a QA incident for a PR-review finding.

    Used by `retrace review` for things like:
      - "PR changes /api/login but no test covers the failing edge case
        from prior incident INC-XX."
      - "Diff modifies the auth flow; the existing replay-derived test
        is now stale."
    """
    try:
        now = utc_now_iso()
        fp = make_fingerprint(["pr_review", repo, str(pr_number), title])
        evidence = IncidentEvidence(primary_url=_pr_url(repo, pr_number))
        incident = Incident(
            id=secrets.token_hex(16),
            public_id=make_public_id(),
            project_id=project_id,
            environment_id=environment_id,
            fingerprint=fp,
            title=title[:240],
            summary=summary[:1024],
            suspected_cause=suspected_cause,
            severity=severity,
            confidence="medium",
            status="open",
            primary_source_kind="error_monitor",
            sources=[
                IncidentSource(
                    kind="error_monitor",
                    ref_id=f"{repo}#{pr_number}",
                    score=1.0,
                    note="pr_review",
                    created_at=now,
                )
            ],
            reproduction=[
                ReproductionStep(
                    index=i,
                    action="inspect_file",
                    description=f"Inspect {f}",
                    target={"file": f},
                )
                for i, f in enumerate(files[:8])
            ],
            expected_outcome="PR includes coverage for the changed flow",
            actual_outcome=summary,
            app_url=_pr_url(repo, pr_number),
            evidence=evidence,
            affected_count=1,
            affected_users=0,
            first_seen_ms=0,
            last_seen_ms=0,
            created_at=now,
            updated_at=now,
        )
        _id, public_id, _inserted = store.upsert_qa_incident_returning(incident.to_row())
        return public_id
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("qa_incident_bridge: failed to file PR review finding: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internal: FailureRow -> Incident
# ---------------------------------------------------------------------------


def _incident_from_failure(failure: FailureRow) -> Incident:
    """Map a monitoring/test failure to the QA incident shape."""
    source_kind = _source_kind_for(failure.source_type)
    metadata = failure.metadata or {}
    primary_url = _primary_url_for_failure(failure, metadata)
    reproduction = _reproduction_for_failure(failure, metadata)
    evidence = _evidence_for_failure(failure, metadata)

    now = utc_now_iso()
    # Always mint a fresh `INC-...` public id. Failures carry their own
    # `flr_*` ids that aren't legible to humans and would collide with
    # the QA-side `INC-...` contract (the upsert is fingerprint-keyed,
    # so reruns reuse the existing row anyway).
    return Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id=failure.project_id,
        environment_id=failure.environment_id,
        fingerprint=failure.fingerprint or make_fingerprint(
            [failure.title, failure.source_type, failure.source_external_id]
        ),
        title=failure.title[:240],
        summary=failure.summary[:1024],
        suspected_cause=str(metadata.get("suspected_cause", "") or ""),
        severity=failure.severity or "medium",
        confidence=failure.confidence or "medium",
        status="open",
        primary_source_kind=source_kind,
        sources=[
            IncidentSource(
                kind=source_kind,
                ref_id=failure.id,
                score=1.0,
                note=f"{failure.source_type}:{failure.source_external_id or '-'}",
                created_at=now,
            )
        ],
        reproduction=reproduction,
        expected_outcome=str(metadata.get("expected_outcome", "") or ""),
        actual_outcome=failure.summary[:1024],
        app_url=primary_url,
        evidence=evidence,
        affected_count=int(failure.affected_sessions or failure.affected_users or 1),
        affected_users=int(failure.affected_users or 0),
        first_seen_ms=int(failure.first_seen_ms or 0),
        last_seen_ms=int(failure.last_seen_ms or 0),
        created_at=now,
        updated_at=now,
    )


def _source_kind_for(source_type: str) -> str:
    """Map a failure's `source_type` into our small enum of source kinds."""
    s = (source_type or "").lower()
    if s.startswith("api_test") or s == "api_test":
        return "api_test"
    if s in {"ui_test", "tester", "browser_harness"}:
        return "ui_test"
    if s in {"replay", "replay_issue"}:
        return "replay"
    return "error_monitor"


def _primary_url_for_failure(failure: FailureRow, metadata: dict[str, Any]) -> str:
    for key in ("url", "request_url", "target_url", "page_url"):
        v = metadata.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _reproduction_for_failure(
    failure: FailureRow,
    metadata: dict[str, Any],
) -> list[ReproductionStep]:
    """Pull a reproduction recipe out of the failure metadata.

    The shapes vary by source. We accept any of:
      - `reproduction_steps`: pre-baked list
      - `request_method` + `request_url`: an API call step
      - `trace`: a stack-frame breadcrumb (last resort)
    """
    raw = metadata.get("reproduction_steps")
    if isinstance(raw, list):
        steps: list[ReproductionStep] = []
        for i, item in enumerate(raw):
            if isinstance(item, str):
                steps.append(
                    ReproductionStep(index=i, action="describe", description=item)
                )
            elif isinstance(item, dict):
                d = dict(item)
                d.setdefault("index", i)
                steps.append(ReproductionStep.from_dict(d))
        if steps:
            return steps

    method = metadata.get("request_method") or metadata.get("method")
    url = metadata.get("request_url") or metadata.get("url")
    if method and url:
        out = [
            ReproductionStep(
                index=0,
                action="api_call",
                description=f"{str(method).upper()} {url}",
                target={"method": str(method).upper(), "url": str(url)},
                url=str(url),
            )
        ]
        expected = metadata.get("expected_status")
        actual = metadata.get("response_status")
        if expected is not None and actual is not None:
            out.append(
                ReproductionStep(
                    index=1,
                    action="assert",
                    description=f"expected HTTP {expected}, got {actual}",
                    target={"expected_status": expected, "actual_status": actual},
                )
            )
        return out

    # Stack trace breadcrumb — better than nothing.
    trace = metadata.get("top_stack_frame") or metadata.get("trace")
    if isinstance(trace, str) and trace.strip():
        return [
            ReproductionStep(
                index=0,
                action="describe",
                description=f"Surface trace: {trace.strip()[:300]}",
            )
        ]
    return []


def _evidence_for_failure(
    failure: FailureRow,
    metadata: dict[str, Any],
) -> IncidentEvidence:
    return IncidentEvidence(
        replay_session_ids=_strs(metadata.get("session_ids")),
        replay_issue_ids=_strs(metadata.get("replay_issue_ids")),
        report_finding_ids=[
            int(x) for x in _strs(metadata.get("report_finding_ids")) if str(x).isdigit()
        ],
        tester_run_ids=_strs(metadata.get("tester_run_ids")),
        api_test_run_ids=_strs(metadata.get("api_test_run_ids")),
        error_issue_ids=_strs(metadata.get("error_issue_ids")),
        trace_ids=_strs(metadata.get("trace_ids")),
        console_excerpts=_strs(metadata.get("console_excerpts")),
        network_failures=[
            x for x in (metadata.get("network_failures") or []) if isinstance(x, dict)
        ],
        primary_url=str(metadata.get("primary_url", "") or _primary_url_for_failure(failure, metadata)),
        replay_url=str(metadata.get("replay_url", "") or ""),
        top_stack_frame=str(metadata.get("top_stack_frame", "") or ""),
    )


def _strs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    if isinstance(value, str):
        return [value]
    return []


def _pr_url(repo: str, pr_number: int) -> str:
    if not repo:
        return ""
    return f"https://github.com/{repo}/pull/{pr_number}"
