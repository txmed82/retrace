"""Transport tests — queue, flush, drop policy, atexit."""

from __future__ import annotations

import time
from typing import Any


from retrace_sdk.transport import Transport


def _make_recorder():
    sent: list[bytes] = []

    def _send(url: str, headers: dict[str, str], body: bytes) -> None:
        sent.append(body)

    return sent, _send


def test_transport_sends_via_worker_thread():
    sent, sender = _make_recorder()
    t = Transport(url="http://test/", public_key="rtpk", sender=sender)
    try:
        t.enqueue(b"payload-1")
        t.enqueue(b"payload-2")
        assert t.flush(timeout=1.0) is True
    finally:
        t.shutdown(timeout=1.0)
    assert sent == [b"payload-1", b"payload-2"]


def test_transport_drops_oldest_on_overflow():
    """Queue size 2 with 4 rapid enqueues — the *freshest* event must
    always survive, and the drop counter must tick.

    We don't pin which earlier events make it through because scheduling
    determines whether the worker grabs `p0` before `p1`/`p2`/`p3`
    arrive. The invariant the drop-policy promises is "fresh wins,"
    not "exactly the last N."
    """
    sent, sender = _make_recorder()

    # Hold the worker on the first item so the queue actually fills.
    block = []

    def _slow(url: str, headers: dict[str, str], body: bytes) -> None:
        if not block:
            block.append(time.monotonic())
            time.sleep(0.2)  # hold up the worker
        sent.append(body)

    t = Transport(url="http://test/", public_key="rtpk", sender=_slow, queue_size=2)
    try:
        for i in range(4):
            t.enqueue(f"p{i}".encode())
        t.flush(timeout=2.0)
    finally:
        t.shutdown(timeout=2.0)
    # Freshest item never dropped — this is the contract.
    assert b"p3" in sent
    # Overflow stat reflects at least some drops (queue size 2, 4 inputs).
    assert t.stats.dropped_overflow >= 1
    # No event is silently lost beyond what overflow recorded.
    assert t.stats.sent + t.stats.dropped_overflow + t.stats.dropped_error == 4


def test_transport_records_send_errors_but_does_not_crash_worker():
    """A failing sender shouldn't kill the thread or freeze the queue."""
    calls = {"n": 0}

    def _flaky(url: str, headers: dict[str, str], body: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first one fails")

    t = Transport(url="http://test/", public_key="rtpk", sender=_flaky)
    try:
        t.enqueue(b"a")
        t.enqueue(b"b")
        t.flush(timeout=1.0)
    finally:
        t.shutdown(timeout=1.0)
    assert calls["n"] == 2
    assert t.stats.dropped_error == 1
    assert "first one fails" in t.stats.last_error
    assert t.stats.sent == 1


def test_transport_headers_carry_sentry_auth():
    captured_headers: dict[str, Any] = {}

    def _spy(url: str, headers: dict[str, str], body: bytes) -> None:
        captured_headers.update(headers)

    t = Transport(url="http://test/", public_key="rtpk_xyz", sender=_spy)
    try:
        t.enqueue(b"payload")
        t.flush(timeout=1.0)
    finally:
        t.shutdown(timeout=1.0)
    assert captured_headers["Content-Type"] == "application/x-sentry-envelope"
    assert "sentry_key=rtpk_xyz" in captured_headers["X-Sentry-Auth"]
    assert "Content-Length" in captured_headers


def test_transport_flush_returns_false_on_timeout():
    """If the worker is blocked, flush honors the timeout."""
    block_forever = []

    def _hang(url: str, headers: dict[str, str], body: bytes) -> None:
        if not block_forever:
            block_forever.append(time.monotonic())
            # Sleep "forever" relative to the test's flush timeout.
            time.sleep(2.0)

    t = Transport(url="http://test/", public_key="rtpk", sender=_hang, queue_size=4)
    try:
        for i in range(3):
            t.enqueue(f"p{i}".encode())
        # Flush should return False — items left in queue.
        # The first one is in-flight; the rest are still queued.
        assert t.flush(timeout=0.1) is False
    finally:
        t.shutdown(timeout=0.1)


def test_transport_shutdown_is_idempotent():
    _, sender = _make_recorder()
    t = Transport(url="http://test/", public_key="rtpk", sender=sender)
    t.enqueue(b"x")
    t.shutdown(timeout=1.0)
    # second call: must not raise / hang.
    t.shutdown(timeout=1.0)


def test_transport_rejects_non_bytes_payloads():
    _, sender = _make_recorder()
    t = Transport(url="http://test/", public_key="rtpk", sender=sender)
    try:
        assert t.enqueue("not-bytes") is False  # type: ignore[arg-type]
        assert t.enqueue(b"") is False
    finally:
        t.shutdown(timeout=1.0)
