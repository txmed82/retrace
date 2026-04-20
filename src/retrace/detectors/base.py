from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Signal:
    session_id: str
    detector: str
    timestamp_ms: int
    url: str
    details: dict[str, Any] = field(default_factory=dict)


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


def iter_with_url(events: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (current_url, event) pairs, tracking the latest Meta href forward."""
    url = ""
    for e in events:
        if e.get("type") == 4:
            data = e.get("data") or {}
            href = data.get("href")
            if isinstance(href, str):
                url = href
        yield url, e
