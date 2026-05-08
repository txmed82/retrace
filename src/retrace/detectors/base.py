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
