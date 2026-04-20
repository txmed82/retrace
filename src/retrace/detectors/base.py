from __future__ import annotations

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
    _REGISTRY[detector.name] = detector
    return detector


def all_detectors() -> list[Detector]:
    return list(_REGISTRY.values())


def get_detector(name: str) -> Detector | None:
    return _REGISTRY.get(name)
