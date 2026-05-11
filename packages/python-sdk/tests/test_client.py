"""Client / capture_* / init lifecycle tests."""

from __future__ import annotations

import json
from typing import Any


import retrace_sdk
from retrace_sdk.client import Client
from retrace_sdk.scope import Scope


def _decode_envelope(body: bytes) -> dict[str, Any]:
    header, item_header, item_body = body.splitlines()
    return {
        "header": json.loads(header),
        "item_header": json.loads(item_header),
        "event": json.loads(item_body),
    }


def test_client_captures_exception(client_factory, fake_transport):
    client = client_factory()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        eid = client.capture_exception(exc)
    assert eid
    client.flush(timeout=1.0)
    rec = fake_transport.sent[-1]
    decoded = _decode_envelope(rec["body"])
    assert decoded["event"]["exception"]["values"][0]["type"] == "ValueError"
    assert decoded["event"]["release"] == "test-release"
    assert decoded["event"]["environment"] == "test"
    assert decoded["header"]["event_id"] == eid


def test_client_captures_message(client_factory, fake_transport):
    client = client_factory()
    eid = client.capture_message("status", level="warning")
    assert eid
    client.flush(timeout=1.0)
    decoded = _decode_envelope(fake_transport.sent[-1]["body"])
    assert decoded["event"]["message"] == "status"
    assert decoded["event"]["level"] == "warning"


def test_capture_exception_with_no_active_exc_returns_none(client_factory, fake_transport):
    """`capture_exception()` outside an `except` block is a no-op."""
    client = client_factory()
    assert client.capture_exception() is None
    client.flush(timeout=1.0)
    assert fake_transport.sent == []


def test_scope_breadcrumbs_attach_to_event(client_factory, fake_transport):
    client = client_factory()
    Scope.current().add_breadcrumb(category="auth", message="login attempt")
    Scope.current().set_user({"id": "u_1"})
    Scope.current().set_tag("phase", "checkout")
    client.capture_message("ping")
    client.flush(timeout=1.0)
    decoded = _decode_envelope(fake_transport.sent[-1]["body"])
    event = decoded["event"]
    assert event["user"] == {"id": "u_1"}
    assert event["tags"]["phase"] == "checkout"
    crumbs = event["breadcrumbs"]["values"]
    assert crumbs[0]["category"] == "auth"


def test_default_tags_merge_under_per_call_tags(client_factory, fake_transport):
    client = client_factory(default_tags={"region": "us-east-1", "env": "default"})
    client.capture_message("x", tags={"env": "override"})
    client.flush(timeout=1.0)
    event = _decode_envelope(fake_transport.sent[-1]["body"])["event"]
    assert event["tags"]["region"] == "us-east-1"
    assert event["tags"]["env"] == "override"


def test_before_send_can_drop_event(client_factory, fake_transport):
    """`before_send` returning None drops the event silently."""
    client = client_factory(before_send=lambda evt: None)
    client.capture_message("dropped")
    client.flush(timeout=1.0)
    assert fake_transport.sent == []


def test_before_send_can_mutate_event(client_factory, fake_transport):
    def _scrub(evt):
        evt["tags"] = dict(evt.get("tags") or {})
        evt["tags"]["scrubbed"] = "yes"
        return evt

    client = client_factory(before_send=_scrub)
    client.capture_message("x")
    client.flush(timeout=1.0)
    event = _decode_envelope(fake_transport.sent[-1]["body"])["event"]
    assert event["tags"]["scrubbed"] == "yes"


def test_init_with_empty_dsn_disables_sdk():
    """Empty DSN → no client, no errors, calls become no-ops."""
    assert retrace_sdk.init(dsn="") is None
    assert retrace_sdk.get_client() is None
    assert retrace_sdk.capture_message("x") is None
    assert retrace_sdk.flush() is True
    retrace_sdk.close()  # must not raise


def test_init_invalid_dsn_disables_silently():
    assert retrace_sdk.init(dsn="not-a-dsn") is None
    assert retrace_sdk.get_client() is None


def test_init_with_empty_dsn_closes_previous_client():
    """Regression for CodeRabbit Major on PR #128: re-`init(dsn="")`
    must shut the existing transport down rather than leak its
    worker thread."""
    # Spin up a real client, then disable.
    first = retrace_sdk.init(dsn="http://rtpk_first@127.0.0.1:0/proj_a")
    assert first is not None
    assert retrace_sdk.get_client() is first
    first_transport = first.transport

    retrace_sdk.init(dsn="")
    assert retrace_sdk.get_client() is None
    # The previous transport thread must have been signalled to stop.
    # `_stop` is the internal Event we use in `shutdown()`.
    assert first_transport._stop.is_set()


def test_init_with_invalid_dsn_closes_previous_client():
    first = retrace_sdk.init(dsn="http://rtpk_first@127.0.0.1:0/proj_a")
    assert first is not None
    first_transport = first.transport
    retrace_sdk.init(dsn="not-a-dsn")
    assert retrace_sdk.get_client() is None
    assert first_transport._stop.is_set()


def test_max_breadcrumbs_setting_resizes_scope():
    """Passing `max_breadcrumbs` through `init()` enforces the cap on
    the current scope (regression for: setting the option on Client
    but not propagating into Scope)."""
    Scope.replace_current(Scope())
    # Fake transport not needed — we don't enqueue.
    from retrace_sdk.transport import Transport
    sent: list[bytes] = []
    transport = Transport(
        url="http://x/",
        public_key="rtpk",
        sender=lambda u, h, b: sent.append(b),
    )
    try:
        client = Client(
            dsn="http://rtpk_a@127.0.0.1/proj",
            max_breadcrumbs=3,
            transport=transport,
        )
        for i in range(10):
            Scope.current().add_breadcrumb(message=f"m{i}")
        assert len(Scope.current().breadcrumbs) == 3
        client.close(timeout=1.0)
    finally:
        transport.shutdown(timeout=1.0)
