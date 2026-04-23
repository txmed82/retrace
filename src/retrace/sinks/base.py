from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class Finding:
    session_id: str
    session_url: str
    title: str
    severity: str
    category: str
    what_happened: str
    likely_cause: str
    reproduction_steps: list[str] = field(default_factory=list)
    confidence: str = "medium"
    detector_signals: list[str] = field(default_factory=list)
    affected_count: int = 1
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    distinct_id: str = ""
    error_issue_ids: list[str] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    top_stack_frame: str = ""
    error_tracking_url: str = ""
    logs_url: str = ""
    first_error_ts_ms: int = 0
    last_error_ts_ms: int = 0


@dataclass
class Cluster:
    fingerprint: str
    session_ids: list[str]
    signal_summary: dict[str, int]
    primary_url: str
    first_seen_ms: int
    last_seen_ms: int

    @property
    def affected_count(self) -> int:
        return len(self.session_ids)


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    sessions_scanned: int
    sessions_with_signals: int
    clusters_found: int
    sessions_errored: int = 0
    cap_hit: bool = False


class Sink(Protocol):
    def write(self, summary: RunSummary, findings: list[Finding]) -> None: ...
