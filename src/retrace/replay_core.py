from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from retrace.clusterer import cluster_sessions
from retrace.detectors import Signal, all_detectors
from retrace.llm.analyst import PROMPT_VERSION, analyze_cluster
from retrace.llm.client import LLMClient
from retrace.sinks.base import Cluster, Finding
from retrace.storage import ReplayIssueUpsertResult, SignalDefinitionRow, Storage


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
    issues_inserted: int = 0
    issues_regressed: int = 0
    regressed_public_ids: tuple[str, ...] = ()
    inserted_public_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayIssueAnalysis:
    finding: Finding
    status: str
    model: str
    prompt_version: str
    created_at: str
    error: str
    evidence: dict[str, Any]


def _definition_map(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    config: ReplaySignalConfig,
) -> dict[str, SignalDefinitionRow]:
    detectors = all_detectors()
    if config.enabled_detectors is not None:
        names = sorted(config.enabled_detectors)
        return {
            name: SignalDefinitionRow(
                id="",
                project_id=project_id,
                environment_id=environment_id,
                detector=name,
                enabled=True,
                run_mode="manual",
                thresholds={},
                prompt={},
                custom_definition="",
                match_count=0,
                last_match_at=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            for name in names
        }
    store.ensure_signal_definitions(
        project_id=project_id,
        environment_id=environment_id,
        detector_names=[detector.name for detector in detectors],
    )
    return {
        definition.detector: definition
        for definition in store.list_signal_definitions(
            project_id=project_id,
            environment_id=environment_id,
            enabled=True,
        )
    }


def _filter_signals_by_definition(
    signals: list[Signal], definition: SignalDefinitionRow | None
) -> list[Signal]:
    if definition is None:
        return signals
    thresholds = definition.thresholds or {}
    try:
        min_matches = int(thresholds.get("min_matches") or 1)
    except (TypeError, ValueError):
        min_matches = 1
    if min_matches > 1 and len(signals) < min_matches:
        return []
    return signals


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


def _signal_evidence(signal: Signal) -> dict[str, Any]:
    return {
        "detector": signal.detector,
        "timestamp_ms": signal.timestamp_ms,
        "url": signal.url,
        "details": signal.details,
    }


def _event_evidence(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    out: dict[str, Any] = {
        "type": event.get("type"),
        "timestamp_ms": int(event.get("timestamp") or 0),
    }
    if isinstance(data, dict):
        for key in ("href", "source", "type", "id", "plugin", "payload"):
            if key in data:
                out["data_type" if key == "type" else key] = data[key]
    return out


def _event_window_around_timestamp(
    events: list[dict[str, Any]],
    timestamp_ms: int | None,
    *,
    before: int = 20,
    after: int = 20,
) -> list[dict[str, Any]]:
    if not events:
        return []
    if timestamp_ms is None:
        return events[: before + after]

    def distance(index: int) -> int:
        return abs(int(events[index].get("timestamp") or 0) - timestamp_ms)

    center = min(range(len(events)), key=distance)
    start = max(0, center - before)
    end = min(len(events), center + after + 1)
    return events[start:end]


def build_replay_evidence(
    *,
    cluster: Cluster,
    events_by_session: dict[str, list[dict[str, Any]]],
    signals_by_session: dict[str, list[Signal]],
) -> dict[str, Any]:
    representative_session_id = cluster.session_ids[0]
    all_signals = [
        signal
        for session_id in cluster.session_ids
        for signal in signals_by_session.get(session_id, [])
    ]
    representative_signals = signals_by_session.get(representative_session_id, [])
    pivot_signal = representative_signals[0] if representative_signals else None
    if pivot_signal is None and all_signals:
        pivot_signal = all_signals[0]
    representative_events = _event_window_around_timestamp(
        events_by_session.get(representative_session_id, []),
        pivot_signal.timestamp_ms if pivot_signal is not None else None,
    )
    return {
        "representative_session_id": representative_session_id,
        "session_ids": cluster.session_ids,
        "affected_count": cluster.affected_count,
        "signal_summary": cluster.signal_summary,
        "signals": [_signal_evidence(signal) for signal in all_signals[:20]],
        "events": [_event_evidence(event) for event in representative_events],
    }


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
        self.signal_definitions = _definition_map(
            store=store,
            project_id=project_id,
            environment_id=environment_id,
            config=self.config,
        )

    def detect_session_signals(
        self,
        *,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> list[Signal]:
        signals: list[Signal] = []
        for detector in all_detectors():
            definition = self.signal_definitions.get(detector.name)
            if definition is None:
                continue
            try:
                detected = detector.detect(session_id, events)
                signals.extend(_filter_signals_by_definition(detected, definition))
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
                counts: dict[str, int] = {}
                for signal in signals:
                    counts[signal.detector] = counts.get(signal.detector, 0) + 1
                self.store.record_signal_definition_matches(
                    project_id=self.project_id,
                    environment_id=self.environment_id,
                    detector_counts=counts,
                )

        clusters = cluster_sessions(
            signals_by_session,
            min_size=max(1, int(self.config.min_cluster_size)),
        )
        issues: list[ReplayIssueUpsertResult] = []
        for cluster in clusters:
            analysis = self._analyze_or_fallback(
                cluster=cluster,
                events_by_session=events_by_session,
                signals_by_session=signals_by_session,
            )
            finding = analysis.finding
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
                    analysis_status=analysis.status,
                    analysis_model=analysis.model,
                    analysis_prompt_version=analysis.prompt_version,
                    analysis_created_at=analysis.created_at,
                    analysis_error=analysis.error,
                    evidence=analysis.evidence,
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
    ) -> ReplayIssueAnalysis:
        evidence = build_replay_evidence(
            cluster=cluster,
            events_by_session=events_by_session,
            signals_by_session=signals_by_session,
        )
        created_at = datetime.now(timezone.utc).isoformat()
        if self.llm_client is not None:
            try:
                finding = analyze_cluster(
                    llm_client=self.llm_client,
                    cluster=cluster,
                    events_by_session=events_by_session,
                    signals_by_session=signals_by_session,
                    session_url_builder=lambda sid: f"retrace://replay/{sid}",
                )
                model = str(getattr(getattr(self.llm_client, "cfg", None), "model", ""))
                return ReplayIssueAnalysis(
                    finding=finding,
                    status="ai",
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    created_at=created_at,
                    error="",
                    evidence=evidence,
                )
            except Exception as exc:
                log.warning("LLM replay analysis failed for %s: %s", cluster.fingerprint, exc)
                finding = summarize_replay_issue(
                    cluster=cluster,
                    events_by_session=events_by_session,
                    signals_by_session=signals_by_session,
                )
                return ReplayIssueAnalysis(
                    finding=finding,
                    status="fallback",
                    model="",
                    prompt_version=PROMPT_VERSION,
                    created_at=created_at,
                    error=str(exc),
                    evidence=evidence,
                )
        finding = summarize_replay_issue(
            cluster=cluster,
            events_by_session=events_by_session,
            signals_by_session=signals_by_session,
        )
        return ReplayIssueAnalysis(
            finding=finding,
            status="fallback",
            model="",
            prompt_version=PROMPT_VERSION,
            created_at=created_at,
            error="llm_unavailable",
            evidence=evidence,
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


def make_replay_finalize_handler(
    *,
    store: Storage,
    config: ReplaySignalConfig | None,
    llm_client: LLMClient | None,
    accumulator: dict[str, Any],
):
    """Return a JobWorker handler that processes a single replay.finalize job.

    `accumulator` is mutated with per-job summary counts so the caller can
    aggregate results across the worker run without re-querying the DB.
    """
    accumulator.setdefault("sessions", 0)
    accumulator.setdefault("issue_count", 0)
    accumulator.setdefault("issues_inserted", 0)
    accumulator.setdefault("issues_regressed", 0)
    accumulator.setdefault("inserted_ids", [])
    accumulator.setdefault("regressed_ids", [])

    def handler(job: Any, payload: dict[str, Any]) -> dict[str, Any]:
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
        accumulator["sessions"] += result.sessions_scanned
        accumulator["issue_count"] += len(result.issues)
        for upsert in result.issues:
            if upsert.inserted:
                accumulator["issues_inserted"] += 1
                accumulator["inserted_ids"].append(upsert.public_id)
            elif upsert.regressed:
                accumulator["issues_regressed"] += 1
                accumulator["regressed_ids"].append(upsert.public_id)
        return {"sessions_scanned": result.sessions_scanned}

    return handler


def process_queued_replay_jobs(
    *,
    store: Storage,
    limit: int = 25,
    project_id: str | None = None,
    config: ReplaySignalConfig | None = None,
    llm_client: LLMClient | None = None,
) -> ReplayJobProcessingResult:
    from retrace.worker import JobWorker

    accumulator: dict[str, Any] = {}
    worker = JobWorker(store)
    worker.register(
        "replay.finalize",
        make_replay_finalize_handler(
            store=store,
            config=config,
            llm_client=llm_client,
            accumulator=accumulator,
        ),
    )
    summary = worker.run_once(
        kinds=["replay.finalize"], limit=limit, project_id=project_id
    )

    return ReplayJobProcessingResult(
        jobs_seen=summary.seen,
        jobs_processed=summary.processed,
        jobs_failed=summary.failed,
        sessions_processed=int(accumulator.get("sessions", 0)),
        issues_created_or_updated=int(accumulator.get("issue_count", 0)),
        issues_inserted=int(accumulator.get("issues_inserted", 0)),
        issues_regressed=int(accumulator.get("issues_regressed", 0)),
        regressed_public_ids=tuple(accumulator.get("regressed_ids", []) or []),
        inserted_public_ids=tuple(accumulator.get("inserted_ids", []) or []),
    )
