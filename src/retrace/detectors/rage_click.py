from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import (
    Signal,
    event_data,
    event_timestamp_ms,
    iter_with_url,
    register,
)


WINDOW_MS = 1000
MIN_CLICKS = 3


def _target_attrs(e: dict[str, Any]) -> dict[str, Any]:
    target = event_data(e).get("target")
    if not isinstance(target, dict):
        return {}
    attrs = target.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _is_disabled_explained(e: dict[str, Any]) -> bool:
    attrs = _target_attrs(e)
    disabled = (
        "disabled" in attrs
        and str(attrs.get("disabled") or "").strip().lower()
        in {"", "1", "true", "disabled", "yes"}
    ) or str(attrs.get("aria-disabled") or "").strip().lower() == "true"
    if not disabled:
        return False
    explanation = (
        attrs.get("title")
        or attrs.get("aria-label")
        or attrs.get("data-disabled-reason")
        or attrs.get("data-tooltip")
    )
    return bool(str(explanation or "").strip())


def _is_click(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    data = event_data(e)
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
            base_tid = event_data(base_ev).get("id")
            base_ts = event_timestamp_ms(base_ev)
            for j in range(i + 1, len(click_indices)):
                jdx = click_indices[j]
                _u, ev = enumerated[jdx]
                tid = event_data(ev).get("id")
                ts = event_timestamp_ms(ev)
                if tid != base_tid:
                    break
                if ts - base_ts > WINDOW_MS:
                    break
                window.append(jdx)
            if len(window) >= MIN_CLICKS:
                emitted_indices.update(window)
                disabled_explained = _is_disabled_explained(base_ev)
                reason_codes = ["rage_click.repeated_same_target"]
                if disabled_explained:
                    reason_codes.append("rage_click.disabled_explained_control")
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=base_ts,
                        url=base_url,
                        details={
                            "click_count": len(window),
                            "target_id": base_tid,
                            "disabled_explained": disabled_explained,
                        },
                        confidence="low" if disabled_explained else "medium",
                        reason_codes=tuple(reason_codes),
                    )
                )
        return out


detector = register(RageClickDetector())
