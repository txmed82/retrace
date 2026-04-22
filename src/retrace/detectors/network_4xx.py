from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, event_data, iter_with_url, register


_IGNORED_STATUSES = {401}


@dataclass
class Network4xxDetector:
    name: str = "network_4xx"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if e.get("type") != 6:
                continue
            data = event_data(e)
            if "network" not in str(data.get("plugin", "")):
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            status = payload.get("status_code") or payload.get("status")
            if not isinstance(status, int) or not 400 <= status < 500:
                continue
            if status in _IGNORED_STATUSES:
                continue
            out.append(
                Signal(
                    session_id=session_id,
                    detector=self.name,
                    timestamp_ms=int(e.get("timestamp") or 0),
                    url=url,
                    details={
                        "status": status,
                        "request_url": payload.get("url", ""),
                        "method": payload.get("method", "GET"),
                    },
                )
            )
        return out


detector = register(Network4xxDetector())
