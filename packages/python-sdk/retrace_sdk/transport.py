"""Background-thread HTTP transport.

A bounded `queue.Queue` is fed by `enqueue(envelope_bytes)`. One worker
thread reads off the queue and POSTs to the envelope endpoint via
`urllib.request` (stdlib — no `httpx`/`requests` dependency on the
host).

Drop policy: when the queue is full, the *oldest* item is discarded
and a per-process counter is bumped. Better to lose the first crash
than miss the most-recent one.

`atexit.register(shutdown)` is wired in `client.py`; tests use
`Transport.flush(timeout=...)` directly.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("retrace_sdk.transport")


DEFAULT_QUEUE_SIZE = 100
DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_FLUSH_TIMEOUT = 2.0


@dataclass
class TransportStats:
    enqueued: int = 0
    sent: int = 0
    dropped_overflow: int = 0
    dropped_error: int = 0
    last_status: int = 0
    last_error: str = ""


class Transport:
    """Owns the worker thread + queue."""

    def __init__(
        self,
        *,
        url: str,
        public_key: str,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        # Used by tests to skip the real network. When set, the function
        # receives (url, headers, body) and may raise to simulate failure.
        sender=None,
    ):
        self.url = url
        self.public_key = public_key
        self.http_timeout = float(http_timeout)
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._stats = TransportStats()
        self._sender = sender or self._real_send
        self._thread = threading.Thread(
            target=self._run,
            name="retrace-sdk-transport",
            daemon=True,
        )
        self._thread.start()

    # ---- public API -----------------------------------------------------

    def enqueue(self, envelope: bytes) -> bool:
        """Non-blocking. Returns True if accepted, False if the queue
        was full (oldest item is dropped to make room)."""
        if not isinstance(envelope, (bytes, bytearray)) or not envelope:
            return False
        try:
            self._queue.put_nowait(bytes(envelope))
            self._stats.enqueued += 1
            return True
        except queue.Full:
            # Drop the oldest item — the freshest crash is more useful.
            try:
                self._queue.get_nowait()
                self._stats.dropped_overflow += 1
                self._queue.put_nowait(bytes(envelope))
                self._stats.enqueued += 1
                return True
            except queue.Empty:
                # Race: queue drained between checks. Try once more.
                try:
                    self._queue.put_nowait(bytes(envelope))
                    self._stats.enqueued += 1
                    return True
                except queue.Full:  # pragma: no cover - extreme race
                    self._stats.dropped_overflow += 1
                    return False

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT) -> bool:
        """Block until the queue is empty or `timeout` elapses.

        Returns True if drained cleanly. We don't `Queue.join()` because
        we don't `task_done()` on each item — instead poll `qsize()`
        which is good enough for atexit.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._queue.empty():
                return True
            time.sleep(0.02)
        return self._queue.empty()

    def shutdown(self, timeout: float = DEFAULT_FLUSH_TIMEOUT) -> None:
        """Drain then stop the worker thread. Idempotent."""
        if self._stop.is_set():
            return
        self.flush(timeout=timeout)
        self._stop.set()
        # Sentinel wakes the worker if the queue is empty.
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover
            pass
        self._thread.join(timeout=max(timeout, 1.0))

    @property
    def stats(self) -> TransportStats:
        return self._stats

    # ---- worker loop ----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                # Shutdown sentinel.
                continue
            try:
                self._sender(self.url, self._headers(len(item)), item)
                self._stats.sent += 1
            except Exception as exc:
                self._stats.dropped_error += 1
                self._stats.last_error = str(exc)
                log.debug("retrace-sdk transport error: %s", exc)

    def _headers(self, content_length: int) -> dict[str, str]:
        # Sentry's auth header is the format the ingest server already
        # parses. `sentry_key` is the public DSN key (the `rtpk_…`).
        auth = (
            f"Sentry sentry_version=7, "
            f"sentry_client=retrace-sdk-python/0.1.0, "
            f"sentry_key={self.public_key}"
        )
        return {
            "Content-Type": "application/x-sentry-envelope",
            "Content-Length": str(content_length),
            "X-Sentry-Auth": auth,
        }

    def _real_send(self, url: str, headers: dict[str, str], body: bytes) -> None:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                self._stats.last_status = int(resp.status)
                # We don't read the body — Retrace ingest replies are
                # small and we don't act on them; draining keeps the
                # connection clean for keep-alive (if the host adds it).
                resp.read()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx still reaches here. Capture the status for
            # observability but don't retry — Sentry SDKs don't either.
            self._stats.last_status = int(exc.code)
            raise
