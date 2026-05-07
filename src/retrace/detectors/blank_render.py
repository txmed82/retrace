from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import (
    Signal,
    event_data,
    event_timestamp_ms,
    normalize_event,
    register,
)


MIN_DWELL_MS = 2000
MAX_NODES = 3


def _count_element_nodes(node: dict[str, Any]) -> int:
    if not isinstance(node, dict):
        return 0
    count = 1 if node.get("type") == 2 else 0
    for k in node.get("childNodes") or []:
        count += _count_element_nodes(k)
    return count


@dataclass
class BlankRenderDetector:
    name: str = "blank_render"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        current_url: str | None = None
        nav_ts: int | None = None
        last_node_count: int | None = None

        def _maybe_emit(end_ts: int) -> None:
            if (
                current_url
                and nav_ts is not None
                and last_node_count is not None
                and end_ts - nav_ts >= MIN_DWELL_MS
                and last_node_count < MAX_NODES
            ):
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=nav_ts,
                        url=current_url,
                        details={
                            "node_count": last_node_count,
                            "dwell_ms": end_ts - nav_ts,
                        },
                    )
                )

        last_ts = 0
        for raw in events:
            e = normalize_event(raw)
            ts = event_timestamp_ms(e)
            last_ts = ts
            t = e.get("type")
            if t == 4:
                _maybe_emit(ts)
                href = event_data(e).get("href")
                if isinstance(href, str):
                    current_url = href
                nav_ts = ts
                last_node_count = None
            elif t == 2:
                root = event_data(e).get("node") or {}
                last_node_count = _count_element_nodes(root)
        if events:
            _maybe_emit(last_ts)
        return out


detector = register(BlankRenderDetector())
