from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from retrace.storage import GitHubReviewRunRow, Storage


REVIEW_TRIGGER_RE = re.compile(r"(?<![\w-])@retrace\s+review(?![\w-])", re.IGNORECASE)
TRUSTED_COMMENT_AUTHOR_ASSOCIATIONS = frozenset(
    {"COLLABORATOR", "MEMBER", "OWNER"}
)


class GitHubWebhookError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class GitHubWebhookResult:
    accepted: bool
    event: str
    action: str
    reason: str = ""
    review_run: GitHubReviewRunRow | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "accepted": self.accepted,
            "event": self.event,
            "action": self.action,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.review_run is not None:
            payload["review_run"] = {
                "id": self.review_run.id,
                "repo_full_name": self.review_run.repo_full_name,
                "pr_number": self.review_run.pr_number,
                "status": self.review_run.status,
            }
        return payload


def verify_github_signature(
    *,
    secret: str,
    body: bytes,
    signature: str,
) -> bool:
    clean_secret = secret.strip()
    clean_signature = signature.strip()
    if not clean_secret or not clean_signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        clean_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, clean_signature)


def handle_github_webhook(
    *,
    store: Storage,
    body: bytes,
    headers: Mapping[str, str],
    webhook_secret: str,
) -> GitHubWebhookResult:
    signature = _header(headers, "X-Hub-Signature-256")
    if not verify_github_signature(
        secret=webhook_secret,
        body=body,
        signature=signature,
    ):
        raise GitHubWebhookError(
            "invalid_signature",
            "Missing or invalid GitHub webhook signature.",
            status=401,
        )
    event = _header(headers, "X-GitHub-Event").strip()
    if not event:
        raise GitHubWebhookError(
            "missing_event",
            "X-GitHub-Event is required.",
            status=400,
        )
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise GitHubWebhookError("invalid_json", "Webhook body is not JSON.") from exc
    if not isinstance(payload, dict):
        raise GitHubWebhookError("invalid_payload", "Webhook payload must be an object.")
    action = str(payload.get("action") or "").strip()
    if event != "issue_comment":
        return GitHubWebhookResult(
            accepted=False,
            event=event,
            action=action,
            reason="ignored_event",
        )
    if action and action != "created":
        return GitHubWebhookResult(
            accepted=False,
            event=event,
            action=action,
            reason="ignored_action",
        )
    issue = payload.get("issue")
    comment = payload.get("comment")
    repository = payload.get("repository")
    if not isinstance(issue, dict) or not isinstance(comment, dict):
        raise GitHubWebhookError(
            "invalid_payload",
            "issue_comment webhook requires issue and comment objects.",
        )
    if "pull_request" not in issue:
        return GitHubWebhookResult(
            accepted=False,
            event=event,
            action=action,
            reason="not_pull_request",
        )
    body_text = str(comment.get("body") or "")
    if not REVIEW_TRIGGER_RE.search(body_text):
        return GitHubWebhookResult(
            accepted=False,
            event=event,
            action=action,
            reason="no_review_trigger",
        )
    repo_full_name = ""
    if isinstance(repository, dict):
        repo_full_name = str(repository.get("full_name") or "").strip()
    if not repo_full_name:
        raise GitHubWebhookError("missing_repository", "repository.full_name is required.")
    try:
        pr_number = int(issue.get("number") or 0)
    except (TypeError, ValueError) as exc:
        raise GitHubWebhookError("invalid_pr_number", "issue.number is invalid.") from exc
    if pr_number <= 0:
        raise GitHubWebhookError("invalid_pr_number", "issue.number is required.")
    author_association = str(comment.get("author_association") or "").strip().upper()
    if author_association not in TRUSTED_COMMENT_AUTHOR_ASSOCIATIONS:
        return GitHubWebhookResult(
            accepted=False,
            event=event,
            action=action,
            reason="untrusted_commenter",
        )
    sender = payload.get("sender")
    installation = payload.get("installation")
    review_run = store.create_github_review_run(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        installation_id=_object_id(installation),
        sender_login=(
            str(sender.get("login") or "").strip() if isinstance(sender, dict) else ""
        ),
        comment_id=_object_id(comment),
        comment_url=str(comment.get("html_url") or comment.get("url") or "").strip(),
        status="queued",
        trigger_phrase="@retrace review",
        metadata={
            "delivery_id": _header(headers, "X-GitHub-Delivery"),
            "comment_node_id": str(comment.get("node_id") or ""),
            "issue_url": str(issue.get("html_url") or issue.get("url") or ""),
        },
    )
    return GitHubWebhookResult(
        accepted=True,
        event=event,
        action=action,
        review_run=review_run,
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return str(value or "")
    return ""


def _object_id(value: object) -> str:
    if isinstance(value, dict):
        raw = value.get("id")
        return "" if raw is None else str(raw).strip()
    return ""
