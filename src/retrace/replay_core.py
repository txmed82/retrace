from __future__ import annotations

import logging
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from retrace.clusterer import cluster_sessions
from retrace.detectors import Detector, Signal, all_detectors
from retrace.llm.analyst import analyze_cluster
from retrace.llm.client import LLMClient
from retrace.sinks.base import Cluster, Finding
from retrace.storage import ReplayIssueUpsertResult, Storage


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplaySignalConfig:
    enabled_detectors: frozenset[str] | None = None
    min_cluster_size: int = 1

    @classmethod
    def from_names(cls, names: Iterable[str] | None) -> "ReplaySignalConfig":
        if names is None:
            return cls()
        return cls(enabled_detectors=frozenset(str(n) for n in names))


@dataclass(frozen=True)
class ReplayProcessingResult:
    sessions_scanned: int
    sessions_with_signals: int
    signals_detected: int
    signals_inserted: int
    issues: list[ReplayIssueUpsertResult]


@dataclass(frozen=True)
class ReplayJobProcessingResult:
    jobs_seen: int
    jobs_processed: int
    jobs_failed: int
    sessions_processed: int
    issues_created_or_updated: int


def _configured_detectors(config: ReplaySignalConfig) -> list[Detector]:
    detectors = all_detectors()
    if config.enabled_detectors is None:
        return detectors
    return [d for d in detectors if d.name in config.enabled_detectors]


def _signal_sentence(signal: Signal) -> str:
    details = signal.details or {}
    message = details.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    request_url = details.get("request_url")
    status = details.get("status")
    if request_url and status:
        return f"{signal.detector} on {request_url} returned {status}"
    return signal.detector.replace("_", " ")


def _severity(signals: list[Signal]) -> str:
    names = {s.detector for s in signals}
    if "network_5xx" in names or "blank_render" in names:
        return "high"
    if "console_error" in names or "error_toast" in names:
        return "medium"
    return "low"


def _action_steps(events: list[dict[str, Any]], limit: int = 6) -> list[str]:
    steps: list[str] = []
    seen: set[str] = set()
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        step = ""
        if event.get("type") == 4 and isinstance(data.get("href"), str):
            step = f"Open {data['href']}"
        elif event.get("type") == 3 and data.get("source") == 2 and data.get("type") == 2:
            step = f"Click element id {data.get('id', 'unknown')}"
        elif event.get("type") == 3 and data.get("source") == 5:
            step = f"Enter text into element id {data.get('id', 'unknown')}"
        if step and step not in seen:
            steps.append(step)
            seen.add(step)
        if len(steps) >= limit:
            break
    return steps or ["Open the replay and follow the recorded user path"]


def summarize_replay_issue(
    *,
    cluster: Cluster,
    events_by_session: dict[str, list[dict[str, Any]]],
    signals_by_session: dict[str, list[Signal]],
) -> Finding:
    session_id = cluster.session_ids[0]
    signals = [
        signal
        for sid in cluster.session_ids
        for signal in signals_by_session.get(sid, [])
    ]
    first_signal = signals[0] if signals else None
    signal_text = _signal_sentence(first_signal) if first_signal else "Replay signal detected"
    detector_names = sorted(cluster.signal_summary)
    title = f"{signal_text[:72]} on replay"
    what_happened = (
        f"Retrace detected {', '.join(detector_names) or 'signals'} across "
        f"{cluster.affected_count} replay session(s). The representative replay "
        f"shows the issue around {cluster.first_seen_ms}ms."
    )
    return Finding(
        session_id=session_id,
        session_url=f"retrace://replay/{session_id}",
        title=title,
        severity=_severity(signals),
        category="functional_error",
        what_happened=what_happened,
        likely_cause="Generated from replay signals; confirm with source traces or code ownership data.",
        reproduction_steps=_action_steps(events_by_session.get(session_id, [])),
        confidence="medium" if signals else "low",
        detector_signals=detector_names,
        affected_count=cluster.affected_count,
        first_seen_ms=cluster.first_seen_ms,
        last_seen_ms=cluster.last_seen_ms,
    )


class ReplayCoreService:
    def __init__(
        self,
        *,
        store: Storage,
        project_id: str,
        environment_id: str,
        config: ReplaySignalConfig | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.environment_id = environment_id
        self.config = config or ReplaySignalConfig()
        self.llm_client = llm_client

    def detect_session_signals(
        self,
        *,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> list[Signal]:
        signals: list[Signal] = []
        for detector in _configured_detectors(self.config):
            try:
                signals.extend(detector.detect(session_id, events))
            except Exception as exc:
                log.warning("detector %s failed on replay %s: %s", detector.name, session_id, exc)
        return signals

    def process_sessions(self, session_ids: Iterable[str]) -> ReplayProcessingResult:
        signals_by_session: dict[str, list[Signal]] = {}
        events_by_session: dict[str, list[dict[str, Any]]] = {}
        scanned = 0
        signals_inserted = 0

        for session_id in dict.fromkeys(str(s) for s in session_ids if str(s)):
            playback = self.store.get_replay_playback(
                project_id=self.project_id,
                environment_id=self.environment_id,
                session_id=session_id,
            )
            if playback is None:
                continue
            scanned += 1
            events_by_session[session_id] = playback.events
            signals = self.detect_session_signals(session_id=session_id, events=playback.events)
            if signals:
                signals_by_session[session_id] = signals
                signals_inserted += self.store.upsert_replay_signals(
                    project_id=self.project_id,
                    environment_id=self.environment_id,
                    signals=signals,
                )

        clusters = cluster_sessions(
            signals_by_session,
            min_size=max(1, int(self.config.min_cluster_size)),
        )
        issues: list[ReplayIssueUpsertResult] = []
        for cluster in clusters:
            finding = self._analyze_or_fallback(
                cluster=cluster,
                events_by_session=events_by_session,
                signals_by_session=signals_by_session,
            )
            issues.append(
                self.store.upsert_replay_issue(
                    project_id=self.project_id,
                    environment_id=self.environment_id,
                    fingerprint=cluster.fingerprint,
                    session_ids=cluster.session_ids,
                    signal_summary=cluster.signal_summary,
                    first_seen_ms=cluster.first_seen_ms,
                    last_seen_ms=cluster.last_seen_ms,
                    title=finding.title,
                    summary=finding.what_happened,
                    likely_cause=finding.likely_cause,
                    reproduction_steps=finding.reproduction_steps,
                    severity=finding.severity,
                    priority=finding.severity,
                    confidence=finding.confidence,
                )
            )

        return ReplayProcessingResult(
            sessions_scanned=scanned,
            sessions_with_signals=len(signals_by_session),
            signals_detected=sum(len(s) for s in signals_by_session.values()),
            signals_inserted=signals_inserted,
            issues=issues,
        )

    def _analyze_or_fallback(
        self,
        *,
        cluster: Cluster,
        events_by_session: dict[str, list[dict[str, Any]]],
        signals_by_session: dict[str, list[Signal]],
    ) -> Finding:
        if self.llm_client is not None:
            try:
                return analyze_cluster(
                    llm_client=self.llm_client,
                    cluster=cluster,
                    events_by_session=events_by_session,
                    signals_by_session=signals_by_session,
                    session_url_builder=lambda sid: f"retrace://replay/{sid}",
                )
            except Exception as exc:
                log.warning("LLM replay analysis failed for %s: %s", cluster.fingerprint, exc)
        return summarize_replay_issue(
            cluster=cluster,
            events_by_session=events_by_session,
            signals_by_session=signals_by_session,
        )


def detect_replay_signals(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    session_id: str,
    config: ReplaySignalConfig | None = None,
) -> list[Signal]:
    service = ReplayCoreService(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        config=config,
    )
    playback = store.get_replay_playback(
        project_id=project_id,
        environment_id=environment_id,
        session_id=session_id,
    )
    if playback is None:
        return []
    signals = service.detect_session_signals(session_id=session_id, events=playback.events)
    store.upsert_replay_signals(
        project_id=project_id,
        environment_id=environment_id,
        signals=signals,
    )
    return signals


def process_replay_sessions(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    session_ids: Iterable[str],
    config: ReplaySignalConfig | None = None,
    llm_client: LLMClient | None = None,
) -> ReplayProcessingResult:
    return ReplayCoreService(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        config=config,
        llm_client=llm_client,
    ).process_sessions(session_ids)


def process_replay_session(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    session_id: str,
    config: ReplaySignalConfig | None = None,
    llm_client: LLMClient | None = None,
) -> list[ReplayIssueUpsertResult]:
    return process_replay_sessions(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        session_ids=[session_id],
        config=config,
        llm_client=llm_client,
    ).issues


def process_queued_replay_jobs(
    *,
    store: Storage,
    limit: int = 25,
    project_id: str | None = None,
    config: ReplaySignalConfig | None = None,
    llm_client: LLMClient | None = None,
) -> ReplayJobProcessingResult:
    jobs = store.list_processing_jobs(
        kind="replay.finalize",
        status="queued",
        project_id=project_id,
        limit=limit,
    )
    seen = 0
    processed = 0
    failed = 0
    sessions = 0
    issue_count = 0

    for job in jobs:
        seen += 1
        job_id = str(job["id"])
        if not store.claim_processing_job(job_id):
            continue
        try:
            payload = json.loads(str(job["payload_json"] or "{}"))
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("replay.finalize job is missing session_id")
            result = process_replay_sessions(
                store=store,
                project_id=str(job["project_id"]),
                environment_id=str(job["environment_id"]),
                session_ids=[session_id],
                config=config,
                llm_client=llm_client,
            )
            sessions += result.sessions_scanned
            issue_count += len(result.issues)
            store.finish_processing_job(job_id=job_id, status="succeeded")
            processed += 1
        except Exception as exc:
            log.exception("replay finalize job %s failed", job_id)
            store.finish_processing_job(
                job_id=job_id,
                status="failed",
                error=str(exc),
            )
            failed += 1

    return ReplayJobProcessingResult(
        jobs_seen=seen,
        jobs_processed=processed,
        jobs_failed=failed,
        sessions_processed=sessions,
        issues_created_or_updated=issue_count,
    )
