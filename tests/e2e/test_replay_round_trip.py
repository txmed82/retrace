"""P3.3 scenario 1 — replay round-trip.

A synthetic rrweb batch hits `/api/sdk/replay` over a real HTTP
connection. The server-side ingest path runs end-to-end: payload
parsing, sdk-key auth, rate-limit consumption, replay-session
upsert, batch persistence. We assert the row landed.

Catches regressions where unit-level changes pass each module's
own tests but break the wiring between modules.
"""

from __future__ import annotations

import json
import time
import uuid
from http.client import HTTPConnection


def _post_replay_batch(api, *, session_id: str, sequence: int = 1) -> tuple[int, dict]:
    body = json.dumps(
        {
            "sessionId": session_id,
            "sequence": sequence,
            "flushType": "normal",
            "distinctId": "e2e",
            "metadata": {"page": "/checkout"},
            "events": [
                {
                    "type": 4,
                    "timestamp": int(time.time() * 1000),
                    "data": {"href": "https://e2e.example.com/checkout"},
                }
            ],
        }
    ).encode("utf-8")
    host, port = api.base_url.removeprefix("http://").split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    conn.request(
        "POST",
        "/api/sdk/replay",
        body=body,
        headers={
            "X-Retrace-Key": api.sdk_key,
            "Content-Type": "application/json",
        },
    )
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    status = response.status
    conn.close()
    return status, (json.loads(text) if text else {})


def test_replay_batch_round_trip(live_api):
    session_id = f"sess-{uuid.uuid4().hex}"

    status, body = _post_replay_batch(live_api, session_id=session_id)
    assert status == 202, body

    # Storage-side: a replay_session and at least one replay_batch
    # landed for this session.
    batches = live_api.store.list_replay_batches(
        project_id=live_api.project_id,
        environment_id=live_api.environment_id,
        session_id=session_id,
    )
    assert len(batches) == 1
    assert batches[0]["sequence"] == 1


def test_replay_batch_rejects_missing_sdk_key(live_api):
    """Wiring regression test: 401 must come back when the SDK key
    is missing. Catches the worst kind of bug — a "no auth needed"
    regression that would let strangers POST replay data."""
    host, port = live_api.base_url.removeprefix("http://").split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    conn.request(
        "POST",
        "/api/sdk/replay",
        body=json.dumps({"sessionId": "x", "events": []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    response = conn.getresponse()
    status = response.status
    response.read()
    conn.close()
    assert status == 401
