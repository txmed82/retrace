from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from retrace.issue_sink_clients import (
    CreatedIssue,
    GitHubClient,
    IssueSinkError,
    LinearClient,
)
from retrace.storage import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssueSinkResult:
    issue_id: str
    issue_public_id: str
    provider: str
    external_id: str
    external_url: str
    created: bool
    payload: dict[str, Any]


def build_issue_sink_payload(
    *,
    issue: Any,
    sessions: list[Any],
    provider: str,
    base_url: str = "",
) -> dict[str, Any]:
    public_id = str(issue["public_id"])
    replay_links = []
    for session in sessions:
        session_id = str(session["session_id"])
        replay_links.append(
            {
                "session_id": session_id,
                "role": str(session["role"]),
                "url": f"{base_url.rstrip('/')}/replays/{session_id}"
                if base_url
                else f"retrace://replay/{session_id}",
            }
        )
    correlation = _correlation_block(issue)
    payload: dict[str, Any] = {
        "provider": provider,
        "source": "retrace",
        "source_issue_id": str(issue["id"]),
        "source_public_id": public_id,
        "title": f"[{public_id}] {str(issue['title'] or 'Replay issue')}",
        "severity": str(issue["severity"]),
        "status": str(issue["status"]),
        "summary": str(issue["summary"]),
        "likely_cause": str(issue["likely_cause"]),
        "affected_count": int(issue["affected_count"]),
        "affected_users": int(issue["affected_users"]),
        "replay_links": replay_links,
        "reproduction_steps": _safe_json_list(issue["reproduction_steps_json"]),
        "evidence": _safe_json_obj(issue["evidence_json"]),
    }
    if correlation:
        payload["correlation"] = correlation
    return payload


def _correlation_block(issue: Any) -> dict[str, Any]:
    """Pull RET-29 correlation fields off the replay issue row, if present.

    Older issue rows (or rows from environments without PostHog wired up) are
    expected to have empty values — we omit the block in that case so we don't
    add noise to the sink payload or rendered markdown.
    """
    cols = _row_keys(issue)
    if "trace_ids_json" not in cols:
        return {}
    trace_ids = _safe_json_list(issue["trace_ids_json"])
    error_issue_ids = _safe_json_list(issue["error_issue_ids_json"])
    error_url = str(issue["error_tracking_url"] or "")
    logs_url = str(issue["logs_url"] or "")
    top_frame = str(issue["top_stack_frame"] or "")
    distinct_id = str(issue["distinct_id"] or "")
    if not any([trace_ids, error_issue_ids, error_url, logs_url, top_frame, distinct_id]):
        return {}
    return {
        "distinct_id": distinct_id,
        "trace_ids": [str(t) for t in trace_ids],
        "error_issue_ids": [str(t) for t in error_issue_ids],
        "top_stack_frame": top_frame,
        "error_tracking_url": error_url,
        "logs_url": logs_url,
    }


def _row_keys(issue: Any) -> set[str]:
    keys = getattr(issue, "keys", None)
    if callable(keys):
        return set(keys())
    if isinstance(issue, dict):
        return set(issue.keys())
    return set()


def render_issue_markdown(payload: dict[str, Any]) -> str:
    """Render the sink payload as a Markdown body for Linear/GitHub."""
    lines: list[str] = []
    lines.append(str(payload.get("summary") or ""))
    lines.append("")
    lines.append(
        f"**Severity:** {payload.get('severity', 'unknown')}  "
        f"**Affected sessions:** {payload.get('affected_count', 0)}  "
        f"**Affected users:** {payload.get('affected_users', 0)}"
    )
    cause = str(payload.get("likely_cause") or "")
    if cause:
        lines.append("")
        lines.append("### Likely cause")
        lines.append(cause)
    steps = payload.get("reproduction_steps") or []
    if steps:
        lines.append("")
        lines.append("### Reproduction steps")
        for i, step in enumerate(steps, start=1):
            lines.append(f"{i}. {step}")
    replay_links = payload.get("replay_links") or []
    if replay_links:
        lines.append("")
        lines.append("### Replays")
        for link in replay_links:
            role = link.get("role") or "session"
            url = link.get("url") or ""
            sid = link.get("session_id") or ""
            lines.append(f"- [{role}] [{sid}]({url})")
    correlation = payload.get("correlation") or {}
    if correlation:
        lines.append("")
        lines.append("### Backend correlation")
        distinct_id = correlation.get("distinct_id") or ""
        if distinct_id:
            lines.append(f"- Distinct ID: `{distinct_id}`")
        trace_ids = correlation.get("trace_ids") or []
        if trace_ids:
            lines.append(f"- Trace IDs: {', '.join(f'`{t}`' for t in trace_ids)}")
        error_ids = correlation.get("error_issue_ids") or []
        if error_ids:
            lines.append(
                f"- Error issues: {', '.join(f'`{t}`' for t in error_ids)}"
            )
        error_url = correlation.get("error_tracking_url") or ""
        if error_url:
            lines.append(f"- [Error tracking]({error_url})")
        logs_url = correlation.get("logs_url") or ""
        if logs_url:
            lines.append(f"- [Logs]({logs_url})")
        top_frame = correlation.get("top_stack_frame") or ""
        if top_frame:
            lines.append(f"- Top stack frame: `{top_frame}`")
    lines.append("")
    lines.append("---")
    lines.append(
        f"_Filed by Retrace · public id `{payload.get('source_public_id', '')}`._"
    )
    return "\n".join(lines).strip() + "\n"


def promote_replay_issue(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    issue_id: str,
    provider: str,
    base_url: str = "",
    external_id: str = "",
    external_url: str = "",
    linear_client: LinearClient | None = None,
    linear_team_id: str = "",
    github_client: GitHubClient | None = None,
    github_repo: str = "",
    labels: list[str] | None = None,
) -> IssueSinkResult:
    provider = provider.strip().lower()
    if provider not in {"linear", "github"}:
        raise ValueError("provider must be linear or github")
    issue = store.get_replay_issue(
        project_id=project_id,
        environment_id=environment_id,
        issue_id=issue_id,
    )
    if issue is None:
        raise ValueError(f"Replay issue not found: {issue_id}")
    sessions = store.list_replay_issue_sessions(str(issue["id"]))
    payload = build_issue_sink_payload(
        issue=issue,
        sessions=sessions,
        provider=provider,
        base_url=base_url,
    )

    existing_id = str(issue["external_ticket_id"] or "")
    existing_url = str(issue["external_ticket_url"] or "")
    if existing_id or existing_url:
        return IssueSinkResult(
            issue_id=str(issue["id"]),
            issue_public_id=str(issue["public_id"]),
            provider=provider,
            external_id=existing_id,
            external_url=existing_url,
            created=False,
            payload=payload,
        )

    final_external_id, final_external_url = _resolve_external_target(
        provider=provider,
        payload=payload,
        explicit_external_id=external_id.strip(),
        explicit_external_url=external_url.strip(),
        linear_client=linear_client,
        linear_team_id=linear_team_id,
        github_client=github_client,
        github_repo=github_repo,
        labels=labels or [],
    )
    success = store.mark_replay_issue_ticket_created(
        str(issue["id"]),
        external_ticket_id=final_external_id,
        external_ticket_url=final_external_url,
    )
    if not success:
        logger.error(
            "Failed to mark replay issue ticket as created: issue_id=%s, external_ticket_id=%s",
            issue["id"],
            final_external_id,
        )
        return IssueSinkResult(
            issue_id=str(issue["id"]),
            issue_public_id=str(issue["public_id"]),
            provider=provider,
            external_id=final_external_id,
            external_url=final_external_url,
            created=False,
            payload=payload,
        )
    return IssueSinkResult(
        issue_id=str(issue["id"]),
        issue_public_id=str(issue["public_id"]),
        provider=provider,
        external_id=final_external_id,
        external_url=final_external_url,
        created=True,
        payload=payload,
    )


def _resolve_external_target(
    *,
    provider: str,
    payload: dict[str, Any],
    explicit_external_id: str,
    explicit_external_url: str,
    linear_client: LinearClient | None,
    linear_team_id: str,
    github_client: GitHubClient | None,
    github_repo: str,
    labels: list[str],
) -> tuple[str, str]:
    if explicit_external_id and explicit_external_url:
        return explicit_external_id, explicit_external_url

    title = str(payload.get("title") or "Retrace replay issue")
    body = render_issue_markdown(payload)
    public_id = str(payload.get("source_public_id") or "")

    if provider == "linear" and linear_client is not None:
        team_id = linear_team_id.strip()
        if not team_id:
            raise IssueSinkError(
                "Linear team id is required when promoting via Linear API"
            )
        created = linear_client.create_issue(
            team_id=team_id,
            title=title,
            description=body,
            labels=labels,
        )
        return _merge_explicit(created, explicit_external_id, explicit_external_url)

    if provider == "github" and github_client is not None:
        repo = github_repo.strip()
        if not repo:
            raise IssueSinkError(
                "GitHub repo is required when promoting via GitHub API"
            )
        created = github_client.create_issue(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
        )
        return _merge_explicit(created, explicit_external_id, explicit_external_url)

    final_external_id = explicit_external_id or _default_external_id(
        provider=provider, public_id=public_id
    )
    final_external_url = explicit_external_url or _default_external_url(
        provider=provider, external_id=final_external_id
    )
    return final_external_id, final_external_url


def _merge_explicit(
    created: CreatedIssue,
    explicit_external_id: str,
    explicit_external_url: str,
) -> tuple[str, str]:
    return (
        explicit_external_id or created.external_id,
        explicit_external_url or created.external_url,
    )


def _default_external_id(*, provider: str, public_id: str) -> str:
    prefix = "LIN" if provider == "linear" else "GH"
    return f"{prefix}-{public_id}"


def _default_external_url(*, provider: str, external_id: str) -> str:
    if provider == "linear":
        return f"linear://issue/{external_id}"
    return f"github://issue/{external_id}"


def _safe_json_list(raw: Any) -> list[Any]:
    import json

    if isinstance(raw, list):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        try:
            value = json.loads(raw or "[]")
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _safe_json_obj(raw: Any) -> dict[str, Any]:
    import json

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        try:
            value = json.loads(raw or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
