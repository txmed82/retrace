from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, iter_with_url, register


WINDOW_MS = 1000
MIN_CLICKS = 3


def _is_click(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    data = e.get("data") or {}
    return data.get("source") == 2 and data.get("type") == 2


@dataclass
class RageClickDetector:
    name: str = "rage_click"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        enumerated: list[tuple[str, dict[str, Any]]] = list(iter_with_url(events))
        out: list[Signal] = []
        click_indices = [i for i, (_u, e) in enumerate(enumerated) if _is_click(e)]
        emitted_indices: set[int] = set()
        for i, idx in enumerate(click_indices):
            if idx in emitted_indices:
                continue
            window = [idx]
            base_url, base_ev = enumerated[idx]
            base_tid = (base_ev["data"] or {}).get("id")
            base_ts = int(base_ev.get("timestamp") or 0)
            for j in range(i + 1, len(click_indices)):
                jdx = click_indices[j]
                _u, ev = enumerated[jdx]
                tid = (ev["data"] or {}).get("id")
                ts = int(ev.get("timestamp") or 0)
                if tid != base_tid:
                    break
                if ts - base_ts > WINDOW_MS:
                    break
                window.append(jdx)
            if len(window) >= MIN_CLICKS:
                emitted_indices.update(window)
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=base_ts,
                        url=base_url,
                        details={
                            "click_count": len(window),
                            "target_id": base_tid,
                        },
                    )
                )
        return out


detector = register(RageClickDetector())
