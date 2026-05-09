from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest

from retrace.commands.api import _handler
from retrace.github_app import GitHubWebhookError, handle_github_webhook
from retrace.storage import Storage


def _store(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    return store


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def _payload(*, comment_body: str = "@retrace review") -> dict[str, object]:
    return {
        "action": "created",
        "installation": {"id": 123},
        "sender": {"login": "octocat"},
        "repository": {"full_name": "acme/web"},
        "issue": {
            "number": 42,
            "html_url": "https://github.com/acme/web/pull/42",
            "pull_request": {"url": "https://api.github.com/repos/acme/web/pulls/42"},
        },
        "comment": {
            "id": 987,
            "node_id": "IC_kwDO",
            "body": comment_body,
            "author_association": "MEMBER",
            "html_url": "https://github.com/acme/web/pull/42#issuecomment-987",
        },
    }


def _headers(secret: str, body: bytes) -> dict[str, str]:
    return {
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": _signature(secret, body),
    }


@contextmanager
def _server(store: Storage, *, webhook_secret: str):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _handler(store, github_webhook_secret=webhook_secret),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_github_pr_comment_mention_queues_review_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "webhook-secret"
    body = json.dumps(_payload()).encode("utf-8")

    result = handle_github_webhook(
        store=store,
        body=body,
        headers=_headers(secret, body),
        webhook_secret=secret,
    )

    assert result.accepted is True
    assert result.review_run is not None
    assert result.review_run.status == "queued"
    assert result.review_run.repo_full_name == "acme/web"
    assert result.review_run.pr_number == 42
    assert result.review_run.installation_id == "123"
    assert result.review_run.sender_login == "octocat"
    stored = store.get_github_review_run(result.review_run.id)
    assert stored is not None
    assert stored.comment_id == "987"
    assert stored.metadata["delivery_id"] == "delivery-1"


def test_github_webhook_rejects_invalid_signature(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = json.dumps(_payload()).encode("utf-8")

    with pytest.raises(GitHubWebhookError) as raised:
        handle_github_webhook(
            store=store,
            body=body,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": "sha256=bad",
            },
            webhook_secret="webhook-secret",
        )

    assert raised.value.code == "invalid_signature"
    assert raised.value.status == 401
    assert store.list_github_review_runs() == []


def test_github_webhook_ignores_non_trigger_comments(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "webhook-secret"
    body = json.dumps(_payload(comment_body="@retrace status")).encode("utf-8")

    result = handle_github_webhook(
        store=store,
        body=body,
        headers=_headers(secret, body),
        webhook_secret=secret,
    )

    assert result.accepted is False
    assert result.reason == "no_review_trigger"
    assert store.list_github_review_runs() == []


def test_github_webhook_ignores_issue_comments_that_are_not_prs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "webhook-secret"
    payload = _payload()
    issue = dict(payload["issue"])  # type: ignore[arg-type]
    issue.pop("pull_request")
    payload["issue"] = issue
    body = json.dumps(payload).encode("utf-8")

    result = handle_github_webhook(
        store=store,
        body=body,
        headers=_headers(secret, body),
        webhook_secret=secret,
    )

    assert result.accepted is False
    assert result.reason == "not_pull_request"
    assert store.list_github_review_runs() == []


def test_github_webhook_ignores_untrusted_commenters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "webhook-secret"
    payload = _payload()
    comment = dict(payload["comment"])  # type: ignore[arg-type]
    comment["author_association"] = "NONE"
    payload["comment"] = comment
    body = json.dumps(payload).encode("utf-8")

    result = handle_github_webhook(
        store=store,
        body=body,
        headers=_headers(secret, body),
        webhook_secret=secret,
    )

    assert result.accepted is False
    assert result.reason == "untrusted_commenter"
    assert store.list_github_review_runs() == []


def test_github_webhook_redelivery_is_idempotent_by_comment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "webhook-secret"
    body = json.dumps(_payload()).encode("utf-8")
    headers = _headers(secret, body)

    first = handle_github_webhook(
        store=store,
        body=body,
        headers=headers,
        webhook_secret=secret,
    )
    second = handle_github_webhook(
        store=store,
        body=body,
        headers=headers,
        webhook_secret=secret,
    )

    assert first.accepted is True
    assert second.accepted is True
    assert first.review_run is not None
    assert second.review_run is not None
    assert second.review_run.id == first.review_run.id
    assert len(store.list_github_review_runs(repo_full_name="acme/web", pr_number=42)) == 1


def test_github_webhook_endpoint_rejects_invalid_signature(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = json.dumps(_payload()).encode("utf-8")

    with _server(store, webhook_secret="webhook-secret") as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/github/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": "sha256=bad",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 401
    assert payload["error"] == "invalid_signature"
    assert store.list_github_review_runs() == []


def test_github_webhook_endpoint_queues_review_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "webhook-secret"
    body = json.dumps(_payload()).encode("utf-8")

    with _server(store, webhook_secret=secret) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/github/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                **_headers(secret, body),
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["accepted"] is True
    assert payload["review_run"]["status"] == "queued"
    assert store.list_github_review_runs(repo_full_name="acme/web", pr_number=42)
