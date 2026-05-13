from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})


def normalize_confidence(value: object) -> str:
    confidence = str(value or "medium").strip().lower()
    return confidence if confidence in CONFIDENCE_LEVELS else "medium"


def normalize_reason_codes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    codes: list[str] = []
    seen: set[str] = set()
    for item in raw:
        code = str(item or "").strip()
        if not code or code in seen:
            continue
        codes.append(code)
        seen.add(code)
    return tuple(codes)


@dataclass(frozen=True)
class Signal:
    session_id: str
    detector: str
    timestamp_ms: int
    url: str
    details: dict[str, Any] = field(default_factory=dict)
    confidence: str = "medium"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        details = dict(self.details or {})
        confidence = normalize_confidence(details.get("confidence") or self.confidence)
        reason_codes = normalize_reason_codes(
            self.reason_codes
            or details.get("reason_codes")
            or details.get("reason_code")
            or (f"{self.detector}.matched",)
        )
        details["confidence"] = confidence
        details["reason_codes"] = list(reason_codes)
        object.__setattr__(self, "details", details)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "reason_codes", reason_codes)


class Detector(Protocol):
    name: str

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]: ...


# ── Shared detector helpers (extracted from dead_click / rage_click) ──────────


def _is_click(e: dict[str, Any]) -> bool:
    """Return True if *e* is an rrweb IncrementalSnapshot click event."""
    if e.get("type") != 3:
        return False
    data = event_data(e)
    return data.get("source") == 2 and data.get("type") == 2


def _target_attrs(e: dict[str, Any]) -> dict[str, Any]:
    """Extract the target element attributes dict from a click event."""
    target = event_data(e).get("target")
    if not isinstance(target, dict):
        return {}
    attrs = target.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _truthy_attr(value: object) -> bool:
    """Check whether an HTML attribute value is truthy in a disabled/ignore context."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"", "1", "true", "disabled", "yes"}


def _is_benign_click(e: dict[str, Any]) -> bool:
    """Return True if a click is on a disabled control or a no-op link."""
    attrs = _target_attrs(e)
    if "disabled" in attrs and _truthy_attr(attrs.get("disabled")):
        return True
    if str(attrs.get("aria-disabled") or "").strip().lower() == "true":
        return True
    if str(attrs.get("data-retrace-ignore") or "").strip().lower() == "true":
        return True
    href = str(attrs.get("href") or "").strip().lower().rstrip(";")
    if href in {"#", "javascript:void(0)", "javascript:"}:
        return True
    return False


# ── Network detector base ─────────────────────────────────────────────────────


@dataclass
class NetworkDetectorBase:
    """Base for 4xx / 5xx detectors — handles event iteration and payload
    extraction.  Subclasses only need to supply *name*, *status_min*,
    *status_max*, *reason_code* and *confidence*."""

    name: str
    status_min: int
    status_max: int
    reason_code: str
    confidence: str = "medium"
    ignored_statuses: frozenset[int] = frozenset()

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if not _is_network(e):
                continue
            data = event_data(e)
            payload = data.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            status = payload.get("status_code") or payload.get("status")
            if not isinstance(status, int):
                continue
            if status < self.status_min or status >= self.status_max:
                continue
            if status in self.ignored_statuses:
                continue
            out.append(
                Signal(
                    session_id=session_id,
                    detector=self.name,
                    timestamp_ms=event_timestamp_ms(e),
                    url=url,
                    details={
                        "status": status,
                        "request_url": payload.get("url", ""),
                        "method": payload.get("method", "GET"),
                        "trace": payload.get("trace")
                        if isinstance(payload.get("trace"), dict)
                        else {},
                    },
                    confidence=self.confidence,
                    reason_codes=(self.reason_code,),
                )
            )
        return out


_REGISTRY: dict[str, Detector] = {}


def register(detector: Detector) -> Detector:
    if detector.name in _REGISTRY:
        raise ValueError(f"detector {detector.name!r} already registered")
    _REGISTRY[detector.name] = detector
    return detector


def all_detectors() -> list[Detector]:
    return list(_REGISTRY.values())


def get_detector(name: str) -> Detector | None:
    return _REGISTRY.get(name)


def normalize_event(e: Any) -> dict[str, Any]:
    return e if isinstance(e, dict) else {}


def event_data(e: Any) -> dict[str, Any]:
    e = normalize_event(e)
    d = e.get("data")
    return d if isinstance(d, dict) else {}


def event_timestamp_ms(e: Any) -> int:
    e = normalize_event(e)
    try:
        return int(e.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def iter_with_url(events: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (current_url, event) pairs, tracking the latest Meta href forward."""
    url = ""
    for raw in events:
        e = normalize_event(raw)
        if e.get("type") == 4:
            data = event_data(e)
            href = data.get("href")
            if isinstance(href, str):
                url = href
        yield url, e


def _is_dom_mutation(e: dict[str, Any]) -> bool:
    """Return True if *e* is an rrweb DOM mutation event."""
    if e.get("type") != 3:
        return False
    d = event_data(e)
    return d.get("source") == 0


def _is_network(e: dict[str, Any]) -> bool:
    """Return True if *e* is an rrweb network plugin event."""
    if e.get("type") != 6:
        return False
    d = event_data(e)
    return "network" in str(d.get("plugin", ""))
