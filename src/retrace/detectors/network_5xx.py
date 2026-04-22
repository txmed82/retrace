from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, event_data, iter_with_url, register


@dataclass
class Network5xxDetector:
    name: str = "network_5xx"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if e.get("type") != 6:
                continue
            data = event_data(e)
            plugin = str(data.get("plugin", ""))
            if "network" not in plugin:
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            status = payload.get("status_code") or payload.get("status")
            if not isinstance(status, int) or not 500 <= status < 600:
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


detector = register(Network5xxDetector())
