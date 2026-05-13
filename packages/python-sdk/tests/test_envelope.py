"""Envelope serialization tests."""

from __future__ import annotations

import json
import sys

from retrace_sdk.envelope import build_envelope_bytes, build_event, new_event_id


def test_new_event_id_is_32_char_hex():
    eid = new_event_id()
    assert len(eid) == 32
    int(eid, 16)  # must parse as hex; raises otherwise


def test_build_event_with_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        event = build_event(exc_info=sys.exc_info(), release="r1", environment="dev")
    assert event["level"] == "error"
    assert event["release"] == "r1"
    assert event["environment"] == "dev"
    values = event["exception"]["values"]
    assert values[0]["type"] == "ValueError"
    assert values[0]["value"] == "boom"
    # At least one frame, including this test file.
    frames = values[0]["stacktrace"]["frames"]
    assert any(f["filename"].endswith("test_envelope.py") for f in frames)


def test_build_event_with_message_only():
    event = build_event(message="hello", level="warning")
    assert event["level"] == "warning"
    assert event["message"] == "hello"
    assert "exception" not in event


def test_build_event_tags_are_stringified():
    event = build_event(message="x", tags={"port": 8080, "drop": None, "service": "api"})
    assert event["tags"] == {"port": "8080", "service": "api"}


def test_build_envelope_bytes_round_trips():
    event = build_event(message="x")
    payload = build_envelope_bytes(event=event, dsn="http://rtpk_a@127.0.0.1/proj")
    # Envelope is line-delimited JSON; header, item-header, item-body.
    lines = payload.splitlines()
    assert len(lines) == 3, lines
    header = json.loads(lines[0])
    item_header = json.loads(lines[1])
    item_body = json.loads(lines[2])
    assert header["event_id"] == event["event_id"]
    assert header["dsn"] == "http://rtpk_a@127.0.0.1/proj"
    assert item_header == {
        "type": "event",
        "content_type": "application/json",
        "length": len(lines[2]),
    }
    assert item_body["message"] == "x"


def test_in_app_filters_site_packages():
    from retrace_sdk.envelope import _is_in_app

    assert _is_in_app("/some/proj/app/views.py")
    assert not _is_in_app("/usr/lib/python3.11/site-packages/foo.py")
    assert not _is_in_app("")
