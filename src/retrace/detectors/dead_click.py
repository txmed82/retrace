from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, iter_with_url, register


FOLLOWUP_WINDOW_MS = 2000


def _is_click(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    d = e.get("data") or {}
    return d.get("source") == 2 and d.get("type") == 2


def _is_dom_mutation(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    d = e.get("data") or {}
    return d.get("source") == 0


def _is_network(e: dict[str, Any]) -> bool:
    if e.get("type") != 6:
        return False
    d = e.get("data") or {}
    return "network" in str(d.get("plugin", ""))


@dataclass
class DeadClickDetector:
    name: str = "dead_click"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        enum = list(iter_with_url(events))
        for i, (url, e) in enumerate(enum):
            if not _is_click(e):
                continue
            click_ts = int(e.get("timestamp") or 0)
            tid = (e.get("data") or {}).get("id")
            had_followup = False
            for _u2, e2 in enum[i + 1:]:
                ts2 = int(e2.get("timestamp") or 0)
                if ts2 - click_ts > FOLLOWUP_WINDOW_MS:
                    break
                if _is_dom_mutation(e2) or _is_network(e2):
                    had_followup = True
                    break
            if not had_followup:
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=click_ts,
                        url=url,
                        details={"target_id": tid},
                    )
                )
        return out


detector = register(DeadClickDetector())
