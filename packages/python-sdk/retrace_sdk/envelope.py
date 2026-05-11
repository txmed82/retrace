"""Build Sentry-compatible envelope bytes.

Retrace's ingest endpoint accepts the standard Sentry envelope format:

    {"event_id":"...","sent_at":"...","dsn":"..."}
    {"type":"event","content_type":"application/json"}
    <event json>

One envelope = one event for our purposes. We don't currently batch.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


def _iso_utc(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    # Match Sentry's microsecond-resolution UTC format.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def new_event_id() -> str:
    """Sentry uses a 32-char lowercase hex string (UUID4 with no
    dashes), and the ingest server expects the same shape."""
    return uuid.uuid4().hex


def build_event(
    *,
    exc_info: Optional[tuple] = None,
    message: str = "",
    level: str = "error",
    release: str = "",
    environment: str = "",
    server_name: str = "",
    platform: str = "python",
    sdk_name: str = "retrace-sdk-python",
    sdk_version: str = "0.1.0",
    tags: Optional[dict[str, Any]] = None,
    user: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the event body. Either `exc_info` (sys.exc_info() tuple)
    or `message` must be supplied. Both at once is allowed; the exception
    becomes the primary signal."""
    event: dict[str, Any] = {
        "event_id": event_id or new_event_id(),
        "timestamp": _iso_utc(),
        "platform": platform,
        "level": str(level or "error"),
        "sdk": {"name": sdk_name, "version": sdk_version},
    }
    if release:
        event["release"] = release
    if environment:
        event["environment"] = environment
    if server_name:
        event["server_name"] = server_name
    if tags:
        event["tags"] = {str(k): str(v) for k, v in tags.items() if v is not None}
    if user:
        event["user"] = dict(user)
    if extra:
        event["extra"] = dict(extra)
    if exc_info:
        event["exception"] = _exception_payload(exc_info)
        # Surface the exception's str as the message so reports without
        # exception viewers (e.g. the qa_incident summary) still read.
        try:
            event.setdefault("message", str(exc_info[1]))
        except Exception:  # pragma: no cover - defensive
            pass
    if message:
        # An explicit message overrides any auto-filled one.
        event["message"] = str(message)
    return event


def _exception_payload(exc_info: tuple) -> dict[str, Any]:
    """Build the `exception.values[0]` shape Sentry expects."""
    import traceback

    exc_type, exc_value, exc_tb = exc_info
    if exc_type is None and exc_value is None:
        return {"values": []}
    frames: list[dict[str, Any]] = []
    if exc_tb is not None:
        for tb_frame, lineno in traceback.walk_tb(exc_tb):
            code = tb_frame.f_code
            frames.append(
                {
                    "filename": code.co_filename,
                    "function": code.co_name,
                    "module": tb_frame.f_globals.get("__name__", ""),
                    "lineno": int(lineno),
                    "in_app": _is_in_app(code.co_filename),
                }
            )
    type_name = getattr(exc_type, "__name__", str(exc_type)) if exc_type else ""
    return {
        "values": [
            {
                "type": type_name,
                "value": "" if exc_value is None else str(exc_value),
                "module": getattr(exc_type, "__module__", "") if exc_type else "",
                "stacktrace": {"frames": frames},
            }
        ]
    }


def _is_in_app(filename: str) -> bool:
    """A rough `in_app` heuristic — true unless the file lives under
    site-packages or the stdlib. Saves the host app most of the noise
    in the SDK report without doing per-project config."""
    if not filename:
        return False
    lowered = filename.replace("\\", "/").lower()
    if "/site-packages/" in lowered:
        return False
    if "/dist-packages/" in lowered:
        return False
    if "/lib/python" in lowered and "site-packages" not in lowered:
        return False
    return True


def build_envelope_bytes(
    *,
    event: dict[str, Any],
    dsn: str,
) -> bytes:
    """Serialize the event into a Sentry envelope:

        header\n
        item-header\n
        item-body\n
    """
    event_id = str(event.get("event_id") or new_event_id())
    header = {
        "event_id": event_id,
        "sent_at": _iso_utc(),
        "dsn": dsn,
        "sdk": event.get("sdk", {"name": "retrace-sdk-python", "version": "0.1.0"}),
    }
    item_body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    item_header = {
        "type": "event",
        "content_type": "application/json",
        "length": len(item_body),
    }
    parts = [
        json.dumps(header, separators=(",", ":")).encode("utf-8"),
        b"\n",
        json.dumps(item_header, separators=(",", ":")).encode("utf-8"),
        b"\n",
        item_body,
        b"\n",
    ]
    return b"".join(parts)
