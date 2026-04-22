from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, event_data, iter_with_url, register


_ERROR_LEVELS = {"error", "assert"}


@dataclass
class ConsoleErrorDetector:
    name: str = "console_error"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if e.get("type") != 6:
                continue
            data = event_data(e)
            if not str(data.get("plugin", "")).startswith("rrweb/console"):
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                payload = {}
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
                    url=url,
                    details={"message": message, "level": level},
                )
            )
        return out


detector = register(ConsoleErrorDetector())
