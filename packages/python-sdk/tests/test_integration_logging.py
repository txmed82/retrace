"""Logging integration tests."""

from __future__ import annotations

import json
import logging
from typing import Any

from retrace_sdk import set_client
from retrace_sdk.integrations.logging import LoggingIntegration
from retrace_sdk.scope import Scope


def _decode(body: bytes) -> dict[str, Any]:
    _, _, item_body = body.splitlines()
    return json.loads(item_body)


def _attach_integration(client, **kwargs):
    integ = LoggingIntegration(**kwargs)
    integ.setup(client)
    return integ


def test_error_logs_become_events(client_factory, fake_transport):
    client = client_factory()
    set_client(client)
    _attach_integration(client, event_level=logging.ERROR)
    log = logging.getLogger("retrace_sdk_test_error")
    log.setLevel(logging.DEBUG)
    log.error("something broke")
    client.flush(timeout=1.0)
    assert len(fake_transport.sent) == 1
    event = _decode(fake_transport.sent[0]["body"])
    assert event["message"] == "something broke"
    assert event["level"] == "error"
    assert event["tags"]["logger"] == "retrace_sdk_test_error"


def test_error_with_exc_info_becomes_exception_event(client_factory, fake_transport):
    client = client_factory()
    set_client(client)
    _attach_integration(client)
    log = logging.getLogger("retrace_sdk_test_exc")
    log.setLevel(logging.DEBUG)
    try:
        raise ValueError("from-log")
    except ValueError:
        log.exception("caught")
    client.flush(timeout=1.0)
    assert len(fake_transport.sent) == 1
    event = _decode(fake_transport.sent[0]["body"])
    assert event["exception"]["values"][0]["type"] == "ValueError"
    assert event["exception"]["values"][0]["value"] == "from-log"


def test_info_logs_become_breadcrumbs_not_events(client_factory, fake_transport):
    client = client_factory()
    set_client(client)
    _attach_integration(client, breadcrumb_level=logging.INFO, event_level=logging.ERROR)
    log = logging.getLogger("retrace_sdk_test_info")
    log.setLevel(logging.DEBUG)
    log.info("user clicked button")
    # No event yet.
    assert fake_transport.sent == []
    # But scope picked it up.
    crumbs = list(Scope.current().breadcrumbs)
    assert any(b.message == "user clicked button" for b in crumbs)


def test_event_level_below_breadcrumb_level_still_captures(client_factory, fake_transport):
    """Regression for CodeRabbit Major on PR #128: with
    `breadcrumb_level=ERROR, event_level=WARNING`, the handler must
    accept WARNING records — earlier code set the handler level to
    `breadcrumb_level`, which would have filtered WARNING out before
    `emit()` could promote it."""
    client = client_factory()
    set_client(client)
    _attach_integration(
        client,
        breadcrumb_level=logging.ERROR,
        event_level=logging.WARNING,
    )
    log = logging.getLogger("retrace_sdk_test_floor")
    log.setLevel(logging.DEBUG)
    log.warning("warn-as-event")
    client.flush(timeout=1.0)
    assert len(fake_transport.sent) == 1
    event = _decode(fake_transport.sent[0]["body"])
    assert event["message"] == "warn-as-event"
    assert event["level"] == "warning"


def test_double_setup_replaces_handler(client_factory):
    """Calling `setup()` twice must not stack handlers."""
    client = client_factory()
    set_client(client)
    _attach_integration(client)
    _attach_integration(client)
    from retrace_sdk.integrations.logging import _RetraceLoggingHandler

    handlers = [h for h in logging.getLogger().handlers if isinstance(h, _RetraceLoggingHandler)]
    assert len(handlers) == 1
