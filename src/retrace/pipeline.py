from __future__ import annotations

import logging
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


log = logging.getLogger(__name__)


def _enabled_detector_names(cfg: RetraceConfig) -> set[str]:
    d = cfg.detectors
    names = set()
    if d.console_error:
        names.add("console_error")
    if d.network_5xx:
        names.add("network_5xx")
    if d.network_4xx:
        names.add("network_4xx")
    if d.rage_click:
        names.add("rage_click")
    if d.dead_click:
        names.add("dead_click")
    if d.error_toast:
        names.add("error_toast")
    if d.blank_render:
        names.add("blank_render")
    if d.session_abandon_on_error:
        names.add("session_abandon_on_error")
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
    started_at = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    run_id = store.start_run()

    status = "ok"
    error_msg: str | None = None
    ids: list[str] = []
    findings: list[Finding] = []
    errors = 0
    cap_hit = False

    try:
        cursor = store.get_last_run_cursor() or (started_at - timedelta(hours=cfg.run.lookback_hours))
        ids = ingester.fetch_since(cursor, max_sessions=cfg.run.max_sessions_per_run)
        cap_hit = len(ids) >= cfg.run.max_sessions_per_run

        enabled = _enabled_detector_names(cfg)
        detectors = [d for d in all_detectors() if d.name in enabled]
        if not detectors and enabled:
            log.warning(
                "no detectors resolved despite enabled=%s — check registration", enabled
            )

        processed_started_at: list[datetime] = []
        for sid in ids:
            try:
                events: list[dict[str, Any]] = ingester.load_events(sid)
                signals: list[Signal] = []
                for d in detectors:
                    try:
                        signals.extend(d.detect(sid, events))
                    except Exception as exc:
                        log.warning("detector %s failed on session %s: %s", d.name, sid, exc)
                if signals:
                    finding = analyze_session(
                        llm_client=llm_client,
                        session_id=sid,
                        session_url=_session_replay_url(cfg, sid),
                        events=events,
                        signals=signals,
                    )
                    findings.append(finding)
                meta = store.get_session(sid)
                if meta is not None:
                    processed_started_at.append(meta.started_at)
            except Exception as exc:
                errors += 1
                log.warning("session %s errored: %s", sid, exc)

        finished_at = datetime.now(timezone.utc)
        summary = RunSummary(
            started_at=started_at,
            finished_at=finished_at,
            sessions_scanned=len(ids),
            sessions_flagged=len(findings),
            sessions_errored=errors,
            cap_hit=cap_hit,
        )

        try:
            sink = MarkdownSink(output_dir=cfg.run.output_dir)
            sink.write(summary, findings)
        except Exception as exc:
            error_msg = f"sink write failed: {exc}"
            status = "error"
            log.error(error_msg)

        # Advance cursor: to min(processed_started_at) when cap hit, else to now.
        if cap_hit and processed_started_at:
            next_cursor = min(processed_started_at)
        else:
            next_cursor = started_at
        store.set_last_run_cursor(next_cursor)

        if errors and status == "ok":
            status = "partial"

        return summary
    except Exception as exc:
        status = "error"
        error_msg = str(exc)
        log.exception("pipeline aborted")
        finished_at = datetime.now(timezone.utc)
        return RunSummary(
            started_at=started_at,
            finished_at=finished_at,
            sessions_scanned=len(ids),
            sessions_flagged=len(findings),
            sessions_errored=errors,
            cap_hit=cap_hit,
        )
    finally:
        store.finish_run(
            run_id,
            sessions_scanned=len(ids),
            findings_count=len(findings),
            status=status,
            error=error_msg,
        )
