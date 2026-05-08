from __future__ import annotations

import re
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
LOADING_DWELL_MS = 8000
MAX_NODES = 3
_LOADING_TEXT_RE = re.compile(
    r"\b(loading|loading\.{3}|spinner|please wait|skeleton)\b",
    re.IGNORECASE,
)


def _count_element_nodes(node: dict[str, Any]) -> int:
    if not isinstance(node, dict):
        return 0
    count = 1 if node.get("type") == 2 else 0
    for k in node.get("childNodes") or []:
        count += _count_element_nodes(k)
    return count


def _gather_text(node: dict[str, Any]) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == 3:
        return str(node.get("textContent") or "")
    return " ".join(_gather_text(k) for k in node.get("childNodes") or [])


def _looks_like_loading(node: dict[str, Any]) -> bool:
    text = _gather_text(node)
    return bool(_LOADING_TEXT_RE.search(text))


@dataclass
class BlankRenderDetector:
    name: str = "blank_render"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        current_url: str | None = None
        nav_ts: int | None = None
        last_node_count: int | None = None
        last_loading_state = False
        low_state_start_ts: int | None = None

        def _maybe_emit(end_ts: int) -> None:
            dwell_ms = end_ts - nav_ts if nav_ts is not None else 0
            low_state_dwell_ms = (
                end_ts - low_state_start_ts if low_state_start_ts is not None else 0
            )
            if (
                current_url
                and nav_ts is not None
                and last_node_count is not None
                and dwell_ms >= MIN_DWELL_MS
                and low_state_dwell_ms >= MIN_DWELL_MS
                and last_node_count < MAX_NODES
                and (not last_loading_state or low_state_dwell_ms >= LOADING_DWELL_MS)
            ):
                reason_codes = ["blank_render.low_node_count_after_dwell"]
                if last_loading_state:
                    reason_codes.append("blank_render.loading_state_exceeded_threshold")
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=nav_ts,
                        url=current_url,
                        details={
                            "node_count": last_node_count,
                            "dwell_ms": dwell_ms,
                            "state_dwell_ms": low_state_dwell_ms,
                            "loading_state": last_loading_state,
                        },
                        confidence="high",
                        reason_codes=tuple(reason_codes),
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
                last_loading_state = False
                low_state_start_ts = None
            elif t == 2:
                root = event_data(e).get("node") or {}
                node_count = _count_element_nodes(root)
                loading_state = _looks_like_loading(root)
                was_low = last_node_count is not None and last_node_count < MAX_NODES
                is_low = node_count < MAX_NODES
                if not is_low:
                    low_state_start_ts = None
                elif not was_low or loading_state != last_loading_state:
                    low_state_start_ts = ts
                last_node_count = node_count
                last_loading_state = loading_state
        if events:
            _maybe_emit(last_ts)
        return out


detector = register(BlankRenderDetector())
