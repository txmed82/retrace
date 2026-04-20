from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from retrace.config import RetraceConfig
from retrace.detectors import Signal, all_detectors
from retrace.ingester import PostHogIngester
from retrace.llm.analyst import analyze_session
from retrace.llm.client import LLMClient
from retrace.sinks.base import Finding, RunSummary
from retrace.sinks.markdown import MarkdownSink
from retrace.storage import Storage


def _enabled_detector_names(cfg: RetraceConfig) -> set[str]:
    d = cfg.detectors
    names = set()
    if d.console_error:
        names.add("console_error")
    if d.network_5xx:
        names.add("network_5xx")
    if d.rage_click:
        names.add("rage_click")
    return names


def _session_replay_url(cfg: RetraceConfig, session_id: str) -> str:
    return (
        f"{cfg.posthog.host.rstrip('/')}/project/{cfg.posthog.project_id}"
        f"/replay/{session_id}"
    )


def run_pipeline(
    *,
    cfg: RetraceConfig,
    store: Storage,
    ingester: PostHogIngester,
    llm_client: LLMClient,
    now: datetime,
) -> RunSummary:
    run_id = store.start_run()
    started_at = now

    cursor = store.get_last_run_cursor() or (now - timedelta(hours=cfg.run.lookback_hours))
    ids = ingester.fetch_since(cursor, max_sessions=cfg.run.max_sessions_per_run)

    enabled = _enabled_detector_names(cfg)
    detectors = [d for d in all_detectors() if d.name in enabled]

    findings: list[Finding] = []
    for sid in ids:
        events: list[dict[str, Any]] = ingester.load_events(sid)
        signals: list[Signal] = []
        for d in detectors:
            signals.extend(d.detect(sid, events))
        if not signals:
            continue
        finding = analyze_session(
            llm_client=llm_client,
            session_id=sid,
            session_url=_session_replay_url(cfg, sid),
            events=events,
            signals=signals,
        )
        findings.append(finding)

    finished_at = datetime.now(timezone.utc)
    summary = RunSummary(
        started_at=started_at,
        finished_at=finished_at,
        sessions_scanned=len(ids),
        sessions_flagged=len(findings),
    )

    sink = MarkdownSink(output_dir=cfg.run.output_dir)
    sink.write(summary, findings)

    store.set_last_run_cursor(now)
    store.finish_run(
        run_id,
        sessions_scanned=summary.sessions_scanned,
        findings_count=summary.sessions_flagged,
        status="ok",
    )
    return summary
