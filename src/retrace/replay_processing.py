from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from retrace.clusterer import cluster_sessions
from retrace.detectors import Signal, all_detectors
from retrace.llm.analyst import analyze_cluster
from retrace.llm.client import LLMClient
from retrace.sinks.base import Cluster, Finding
from retrace.storage import ReplayIssueUpsertResult, Storage


@dataclass(frozen=True)
class ReplaySignalConfig:
    enabled_detectors: frozenset[str] | None = None

    @classmethod
    def from_names(cls, names: Iterable[str] | None) -> "ReplaySignalConfig":
        if names is None:
            return cls()
        return cls(enabled_detectors=frozenset(str(n) for n in names))


def _configured_detectors(config: ReplaySignalConfig) -> list[Any]:
    detectors = all_detectors()
    if config.enabled_detectors is None:
        return detectors
    return [d for d in detectors if d.name in config.enabled_detectors]


def detect_replay_signals(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    session_id: str,
    config: ReplaySignalConfig | None = None,
) -> list[Signal]:
    playback = store.get_replay_playback(
        project_id=project_id,
        environment_id=environment_id,
        session_id=session_id,
    )
    if playback is None:
        return []

    rules = config or ReplaySignalConfig()
    signals: list[Signal] = []
    for detector in _configured_detectors(rules):
        signals.extend(detector.detect(session_id, playback.events))
    store.upsert_replay_signals(
        project_id=project_id,
        environment_id=environment_id,
        signals=signals,
    )
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
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event.get("type") == 4 and isinstance(data.get("href"), str):
            steps.append(f"Open {data['href']}")
        elif event.get("type") == 3 and data.get("source") == 2 and data.get("type") == 2:
            steps.append(f"Click element id {data.get('id', 'unknown')}")
        elif event.get("type") == 3 and data.get("source") == 5:
            steps.append(f"Enter text into element id {data.get('id', 'unknown')}")
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
    signals = signals_by_session.get(session_id, [])
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


def process_replay_session(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    session_id: str,
    config: ReplaySignalConfig | None = None,
    llm_client: LLMClient | None = None,
) -> list[ReplayIssueUpsertResult]:
    playback = store.get_replay_playback(
        project_id=project_id,
        environment_id=environment_id,
        session_id=session_id,
    )
    if playback is None:
        return []

    signals = detect_replay_signals(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        session_id=session_id,
        config=config,
    )
    if not signals:
        return []

    signals_by_session = {session_id: signals}
    events_by_session = {session_id: playback.events}
    clusters = cluster_sessions(signals_by_session, min_size=1)
    results: list[ReplayIssueUpsertResult] = []
    for cluster in clusters:
        if llm_client is not None:
            finding = analyze_cluster(
                llm_client=llm_client,
                cluster=cluster,
                events_by_session=events_by_session,
                signals_by_session=signals_by_session,
                session_url_builder=lambda sid: f"retrace://replay/{sid}",
            )
        else:
            finding = summarize_replay_issue(
                cluster=cluster,
                events_by_session=events_by_session,
                signals_by_session=signals_by_session,
            )
        results.append(
            store.upsert_replay_issue(
                project_id=project_id,
                environment_id=environment_id,
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
    return results
