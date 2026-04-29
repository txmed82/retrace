from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from retrace.config import NotificationConfig
from retrace.notification_sinks import (
    NotificationEvent,
    NotificationPayload,
    SlackSink,
    WebhookSink,
    build_sinks_from_config,
    close_sinks,
    dispatch_notification,
)


def _payload(**overrides: Any) -> NotificationPayload:
    base = {
        "event": NotificationEvent.ISSUE_CREATED.value,
        "title": "Replay issue",
        "summary": "Page crashes after pay",
        "severity": "high",
        "public_id": "bug_xyz",
        "url": "https://retrace.example/issues/bug_xyz",
        "extra": {"affected": 3},
    }
    base.update(overrides)
    return NotificationPayload(**base)


# --------------------------- WebhookSink --------------------------------


def test_webhook_sink_posts_payload_with_hmac_signature() -> None:
    import hashlib
    import hmac as _hmac

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["raw_body"] = request.content
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        sink = WebhookSink(
            url="https://hook.example/retrace",
            client=raw,
            secret="shhh",
        )
        result = sink.send(_payload())

    assert result.ok is True
    assert captured["url"] == "https://hook.example/retrace"
    assert captured["body"]["event"] == "issue.created"

    timestamp = captured["headers"]["x-retrace-timestamp"]
    signature = captured["headers"]["x-retrace-signature"]
    assert signature.startswith("sha256=")

    # Receiver-side verification: digest of "<ts>.<body>" with the shared secret.
    expected = _hmac.new(
        b"shhh",
        (timestamp + ".").encode("utf-8") + captured["raw_body"],
        hashlib.sha256,
    ).hexdigest()
    assert signature == f"sha256={expected}"


def test_webhook_sink_omits_signature_when_no_secret() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        sink = WebhookSink(url="https://hook.example/x", client=raw)
        sink.send(_payload())

    assert "x-retrace-signature" not in captured["headers"]
    assert "x-retrace-timestamp" in captured["headers"]


def test_webhook_sink_records_failure_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream bad")

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        sink = WebhookSink(url="https://hook.example", client=raw)
        result = sink.send(_payload())

    assert result.ok is False
    assert result.status_code == 503
    assert "upstream bad" in result.error


def test_webhook_sink_rejects_blank_url() -> None:
    with pytest.raises(ValueError):
        WebhookSink(url="   ")


# --------------------------- SlackSink ---------------------------------


def test_slack_sink_formats_blocks() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        sink = SlackSink(webhook_url="https://hooks.slack/x", client=raw)
        result = sink.send(_payload(event=NotificationEvent.TICKET_CREATED.value))

    assert result.ok is True
    body = captured["body"]
    assert "blocks" in body
    assert ":pencil:" in body["text"]  # ticket.created emoji
    assert "bug_xyz" in body["text"]
    assert "<https://retrace.example/issues/bug_xyz|Open in Retrace>" in body["text"]


def test_slack_sink_uses_default_emoji_for_unknown_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert ":mag:" in body["text"]
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as raw:
        sink = SlackSink(webhook_url="https://hooks.slack/x", client=raw)
        sink.send(_payload(event="unknown.event"))


# --------------------------- dispatch + build ---------------------------


def test_dispatch_notification_swallows_sink_exceptions() -> None:
    class Boom:
        name = "boom"

        def send(self, payload: NotificationPayload) -> Any:
            raise RuntimeError("nope")

    results = dispatch_notification([Boom()], _payload())
    assert len(results) == 1
    assert results[0].ok is False
    assert "nope" in results[0].error


def test_build_sinks_from_config_empty_when_disabled() -> None:
    cfg = NotificationConfig()
    assert build_sinks_from_config(cfg) == []
    assert cfg.enabled is False


def test_build_sinks_from_config_includes_both_when_set() -> None:
    cfg = NotificationConfig(
        webhook_url="https://hook.example/x",
        slack_webhook_url="https://hooks.slack/y",
    )
    sinks = build_sinks_from_config(cfg)
    try:
        assert len(sinks) == 2
        names = {s.name for s in sinks}
        assert names == {"webhook", "slack"}
    finally:
        close_sinks(sinks)


def test_close_sinks_swallows_per_sink_close_errors() -> None:
    class FlakyClose:
        name = "flaky"

        def close(self) -> None:
            raise RuntimeError("close failed")

    # Should not raise.
    close_sinks([FlakyClose()])
