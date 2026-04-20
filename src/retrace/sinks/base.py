from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class Finding:
    session_id: str
    session_url: str
    title: str
    severity: str            # critical | high | medium | low
    category: str            # functional_error | visual_bug | performance | confusion
    what_happened: str
    likely_cause: str
    reproduction_steps: list[str] = field(default_factory=list)
    confidence: str = "medium"
    detector_signals: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    sessions_scanned: int
    sessions_flagged: int
    sessions_errored: int = 0
    cap_hit: bool = False


class Sink(Protocol):
    def write(self, summary: RunSummary, findings: list[Finding]) -> None: ...
