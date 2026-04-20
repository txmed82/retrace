from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, iter_with_url, register


ABANDON_WINDOW_MS = 5000


def _is_error_ish(e: dict[str, Any]) -> bool:
    if e.get("type") != 6:
        return False
    d = e.get("data") or {}
    plugin = str(d.get("plugin", ""))
    payload = d.get("payload") or {}
    if plugin.startswith("rrweb/console"):
        return payload.get("level") in {"error", "assert"}
    if "network" in plugin:
        status = payload.get("status_code") or payload.get("status")
        return isinstance(status, int) and 400 <= status < 600
    return False


@dataclass
class SessionAbandonDetector:
    name: str = "session_abandon_on_error"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        if not events:
            return []
        last_ts = int(events[-1].get("timestamp") or 0)
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if _is_error_ish(e):
                ts = int(e.get("timestamp") or 0)
                if last_ts - ts <= ABANDON_WINDOW_MS:
                    out.append(
                        Signal(
                            session_id=session_id,
                            detector=self.name,
                            timestamp_ms=ts,
                            url=url,
                            details={"time_to_end_ms": last_ts - ts},
                        )
                    )
                    break
        return out


detector = register(SessionAbandonDetector())
