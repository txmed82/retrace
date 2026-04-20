from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, register


_ERROR_LEVELS = {"error", "assert"}


def _current_url(events: list[dict[str, Any]], idx: int) -> str:
    for i in range(idx, -1, -1):
        e = events[i]
        if e.get("type") == 4 and "href" in (e.get("data") or {}):
            return e["data"]["href"]
    return ""


@dataclass
class ConsoleErrorDetector:
    name: str = "console_error"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for idx, e in enumerate(events):
            if e.get("type") != 6:
                continue
            data = e.get("data") or {}
            if not str(data.get("plugin", "")).startswith("rrweb/console"):
                continue
            payload = data.get("payload") or {}
            level = payload.get("level")
            if level not in _ERROR_LEVELS:
                continue
            msg_parts = payload.get("payload") or []
            message = " ".join(str(p) for p in msg_parts)
            out.append(
                Signal(
                    session_id=session_id,
                    detector=self.name,
                    timestamp_ms=int(e.get("timestamp") or 0),
                    url=_current_url(events, idx),
                    details={"message": message, "level": level},
                )
            )
        return out


detector = register(ConsoleErrorDetector())
