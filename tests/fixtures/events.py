"""
Builders for minimal rrweb-shaped event streams.

rrweb event types we care about:
  2 = FullSnapshot
  3 = IncrementalSnapshot (source 2 = MouseInteraction, source 5 = Input)
  6 = Plugin (console, network)
"""
from __future__ import annotations

from typing import Any


def meta(ts: int = 0, href: str = "https://example.com/") -> dict[str, Any]:
    return {"type": 4, "timestamp": ts, "data": {"href": href}}


def console_event(ts: int, level: str, message: str) -> dict[str, Any]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "rrweb/console@1",
            "payload": {"level": level, "payload": [message], "trace": []},
        },
    }


def network_event(
    ts: int, url: str, status: int, method: str = "GET"
) -> dict[str, Any]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "posthog/network@1",
            "payload": {
                "method": method,
                "url": url,
                "status_code": status,
            },
        },
    }


def click_event(ts: int, x: int, y: int, target_id: int = 42) -> dict[str, Any]:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {"source": 2, "type": 2, "id": target_id, "x": x, "y": y},
    }
