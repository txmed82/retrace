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


FOLLOWUP_WINDOW_MS = 2000


def _target_attrs(e: dict[str, Any]) -> dict[str, Any]:
    target = event_data(e).get("target")
    if not isinstance(target, dict):
        return {}
    attrs = target.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _truthy_attr(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"", "1", "true", "disabled", "yes"}


def _is_benign_click(e: dict[str, Any]) -> bool:
    attrs = _target_attrs(e)
    if "disabled" in attrs and _truthy_attr(attrs.get("disabled")):
        return True
    if str(attrs.get("aria-disabled") or "").strip().lower() == "true":
        return True
    if str(attrs.get("data-retrace-ignore") or "").strip().lower() == "true":
        return True
    href = str(attrs.get("href") or "").strip()
    if href in {"#", "javascript:void(0)", "javascript:;"}:
        return True
    return False


def _is_click(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    d = event_data(e)
    return d.get("source") == 2 and d.get("type") == 2


def _is_dom_mutation(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    d = event_data(e)
    return d.get("source") == 0


def _is_network(e: dict[str, Any]) -> bool:
    if e.get("type") != 6:
        return False
    d = event_data(e)
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
