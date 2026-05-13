from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import (
    Signal,
    _is_benign_click,
    _is_click,
    _is_dom_mutation,
    _is_network,
    event_data,
    event_timestamp_ms,
    iter_with_url,
    register,
)


FOLLOWUP_WINDOW_MS = 2000


@dataclass
class DeadClickDetector:
    name: str = "dead_click"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        enum = list(iter_with_url(events))
        for i, (url, e) in enumerate(enum):
            if not _is_click(e):
                continue
            if _is_benign_click(e):
                continue
            click_ts = event_timestamp_ms(e)
            tid = event_data(e).get("id")
            had_followup = False
            for _u2, e2 in enum[i + 1 :]:
                ts2 = event_timestamp_ms(e2)
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
                        confidence="medium",
                        reason_codes=("dead_click.no_followup_dom_or_network",),
                    )
                )
        return out


detector = register(DeadClickDetector())
