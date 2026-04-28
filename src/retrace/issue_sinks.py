from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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
                "url": f"{base_url.rstrip('/')}/replays/{session_id}" if base_url else f"retrace://replay/{session_id}",
            }
        )
    return {
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

    final_external_id = external_id.strip() or _default_external_id(
        provider=provider,
        public_id=str(issue["public_id"]),
    )
    final_external_url = external_url.strip() or _default_external_url(
        provider=provider,
        external_id=final_external_id,
    )
    success = store.mark_replay_issue_ticket_created(
        str(issue["id"]),
        external_ticket_id=final_external_id,
        external_ticket_url=final_external_url,
    )
    if not success:
        logger.error(
            "Failed to mark replay issue ticket as created: issue_id=%s, external_ticket_id=%s, external_ticket_url=%s",
            issue["id"],
            final_external_id,
            final_external_url,
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
