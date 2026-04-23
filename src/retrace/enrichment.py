from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from retrace.config import RetraceConfig
from retrace.detectors.base import Signal
from retrace.sinks.base import Finding
from retrace.storage import Storage


log = logging.getLogger(__name__)


class CorrelationEnricher:
    """Best-effort observability correlation for flagged sessions.

    This uses PostHog's query API and is intentionally failure tolerant:
    timeouts, auth issues, and query errors should never fail the pipeline run.
    """

    def __init__(
        self,
        cfg: RetraceConfig,
        store: Storage,
        *,
        connect_timeout_s: float = 2.0,
        read_timeout_s: float = 4.0,
        max_retries: int = 2,
    ):
        self.cfg = cfg
        self.store = store
        self.connect_timeout_s = max(0.5, float(connect_timeout_s))
        self.read_timeout_s = max(1.0, float(read_timeout_s))
        self.max_retries = max(1, int(max_retries))
        self.query_host = self._query_host(cfg.posthog.host)
        self.query_url = (
            f"{self.query_host.rstrip('/')}/api/projects/{cfg.posthog.project_id}/query/"
        )

    def enrich(self, finding: Finding, signals: list[Signal]) -> Finding:
        meta = self.store.get_session(finding.session_id)
        distinct_id = meta.distinct_id if meta and meta.distinct_id else ""
        first_error_ts_ms, last_error_ts_ms = self._error_window(signals)
        if first_error_ts_ms == 0 and last_error_ts_ms == 0:
            # Fall back to detector window when no explicit error detector fired.
            first_error_ts_ms, last_error_ts_ms = self._signal_window(signals)
        query_from_ms, query_to_ms = self._expanded_window(first_error_ts_ms, last_error_ts_ms)

        issue_ids: list[str] = []
        trace_ids: list[str] = []
        top_stack_frame = ""

        if self._can_query():
            try:
                exceptions = self._fetch_exception_rows(
                    session_id=finding.session_id,
                    distinct_id=distinct_id,
                    from_ms=query_from_ms,
                    to_ms=query_to_ms,
                )
                issue_ids, trace_ids, top_stack_frame = self._extract_exception_correlation(
                    exceptions
                )
                if first_error_ts_ms == 0 and last_error_ts_ms == 0:
                    first_error_ts_ms, last_error_ts_ms = self._timestamp_bounds_from_rows(
                        exceptions
                    )
            except Exception as exc:
                log.warning(
                    "enrichment: exception correlation failed for session %s: %s",
                    finding.session_id,
                    exc,
                )

            try:
                log_rows = self._fetch_log_rows(
                    session_id=finding.session_id,
                    distinct_id=distinct_id,
                    trace_ids=trace_ids,
                    from_ms=query_from_ms,
                    to_ms=query_to_ms,
                )
                trace_ids = self._merge_trace_ids(
                    trace_ids, [self._first_nonempty(r, "trace_id", "$trace_id") for r in log_rows]
                )
            except Exception as exc:
                log.warning(
                    "enrichment: log correlation failed for session %s: %s",
                    finding.session_id,
                    exc,
                )

        return replace(
            finding,
            distinct_id=distinct_id,
            error_issue_ids=issue_ids,
            trace_ids=trace_ids,
            top_stack_frame=top_stack_frame,
            error_tracking_url=self._error_tracking_url(
                session_id=finding.session_id,
                distinct_id=distinct_id,
                issue_ids=issue_ids,
            ),
            logs_url=self._logs_url(
                session_id=finding.session_id,
                distinct_id=distinct_id,
                trace_ids=trace_ids,
            ),
            first_error_ts_ms=int(first_error_ts_ms),
            last_error_ts_ms=int(last_error_ts_ms),
        )

    def _can_query(self) -> bool:
        return bool(self.cfg.posthog.api_key.strip())

    @staticmethod
    def _query_host(host: str) -> str:
        h = host.rstrip("/")
        if "://us.i.posthog.com" in h:
            return h.replace("://us.i.posthog.com", "://us.posthog.com")
        if "://eu.i.posthog.com" in h:
            return h.replace("://eu.i.posthog.com", "://eu.posthog.com")
        return h

    def _fetch_exception_rows(
        self,
        *,
        session_id: str,
        distinct_id: str,
        from_ms: int,
        to_ms: int,
    ) -> list[dict[str, Any]]:
        cond = [f"properties.$session_id = {self._sql_quote(session_id)}"]
        if distinct_id:
            cond.append(f"distinct_id = {self._sql_quote(distinct_id)}")
        where = " OR ".join(cond)
        query = f"""
        SELECT
          timestamp,
          properties.$exception_fingerprint AS issue_id,
          properties.$exception_list AS exception_list,
          properties.$trace_id AS trace_id,
          properties.trace_id AS trace_id_alt
        FROM events
        WHERE event = '$exception'
          AND timestamp >= fromUnixTimestamp64Milli({int(from_ms)})
          AND timestamp <= fromUnixTimestamp64Milli({int(to_ms)})
          AND ({where})
        ORDER BY timestamp DESC
        LIMIT 50
        """
        return self._query_hogql_rows(query=query, name="retrace-enrichment-exceptions")

    def _fetch_log_rows(
        self,
        *,
        session_id: str,
        distinct_id: str,
        trace_ids: list[str],
        from_ms: int,
        to_ms: int,
    ) -> list[dict[str, Any]]:
        cond = [
            f"properties.$session_id = {self._sql_quote(session_id)}",
            f"properties.session_id = {self._sql_quote(session_id)}",
        ]
        if distinct_id:
            cond.append(f"distinct_id = {self._sql_quote(distinct_id)}")
            cond.append(f"properties.distinct_id = {self._sql_quote(distinct_id)}")
        for tid in trace_ids[:5]:
            cond.append(f"properties.$trace_id = {self._sql_quote(tid)}")
            cond.append(f"properties.trace_id = {self._sql_quote(tid)}")
        where = " OR ".join(cond)
        query = f"""
        SELECT
          timestamp,
          event,
          properties.$trace_id AS trace_id,
          properties.trace_id AS trace_id_alt,
          properties.level AS level,
          properties.severity_text AS severity_text,
          properties.message AS message,
          properties.msg AS msg
        FROM events
        WHERE timestamp >= fromUnixTimestamp64Milli({int(from_ms)})
          AND timestamp <= fromUnixTimestamp64Milli({int(to_ms)})
          AND ({where})
          AND event IN ('$log_entry', '$log', '$otel_log')
        ORDER BY timestamp DESC
        LIMIT 25
        """
        return self._query_hogql_rows(query=query, name="retrace-enrichment-logs")

    def _query_hogql_rows(self, *, query: str, name: str) -> list[dict[str, Any]]:
        payload = {"query": {"kind": "HogQLQuery", "query": query}, "name": name}
        headers = {
            "Authorization": f"Bearer {self.cfg.posthog.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            read=self.read_timeout_s,
            write=10.0,
            pool=10.0,
        )
        err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(self.query_url, headers=headers, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    err = RuntimeError(f"query api status={resp.status_code}")
                    continue
                resp.raise_for_status()
                body = resp.json()
                return self._coerce_rows(body)
            except Exception as exc:
                err = exc
                continue
        if err:
            raise err
        return []

    @staticmethod
    def _coerce_rows(body: Any) -> list[dict[str, Any]]:
        if not isinstance(body, dict):
            return []
        results = body.get("results")
        if not isinstance(results, list):
            return []
        if not results:
            return []

        first = results[0]
        if isinstance(first, dict):
            return [r for r in results if isinstance(r, dict)]

        columns = body.get("columns")
        if (
            isinstance(columns, list)
            and all(isinstance(c, str) for c in columns)
            and isinstance(first, list)
        ):
            out: list[dict[str, Any]] = []
            for row in results:
                if not isinstance(row, list):
                    continue
                out.append(
                    {str(columns[i]): row[i] for i in range(min(len(columns), len(row)))}
                )
            return out
        return []

    @staticmethod
    def _extract_exception_correlation(
        rows: list[dict[str, Any]],
    ) -> tuple[list[str], list[str], str]:
        issue_ids: list[str] = []
        trace_ids: list[str] = []
        top_stack_frame = ""
        for row in rows:
            issue = CorrelationEnricher._first_nonempty(row, "issue_id", "$exception_fingerprint")
            if issue and issue not in issue_ids:
                issue_ids.append(issue)
            trace_id = CorrelationEnricher._first_nonempty(row, "trace_id", "trace_id_alt", "$trace_id")
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
            if not top_stack_frame:
                top_stack_frame = CorrelationEnricher._stack_from_exception_list(
                    row.get("exception_list")
                )
        return issue_ids[:10], trace_ids[:10], top_stack_frame

    @staticmethod
    def _first_nonempty(row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _stack_from_exception_list(value: Any) -> str:
        if not isinstance(value, list) or not value:
            return ""
        first = value[0]
        if not isinstance(first, dict):
            return ""
        stacktrace = first.get("stacktrace")
        if not isinstance(stacktrace, dict):
            return ""
        frames = stacktrace.get("frames")
        if not isinstance(frames, list) or not frames:
            return ""
        frame = frames[-1] if frames else {}
        if not isinstance(frame, dict):
            return ""
        filename = str(frame.get("filename") or "")
        function = str(frame.get("function") or "")
        lineno = frame.get("lineno")
        colno = frame.get("colno")
        loc = f"{filename}:{lineno}" if filename and lineno is not None else filename
        if colno is not None and loc:
            loc = f"{loc}:{colno}"
        bits = [b for b in [function, loc] if b]
        return " @ ".join(bits)[:240] if bits else ""

    @staticmethod
    def _merge_trace_ids(seed: list[str], others: list[str]) -> list[str]:
        out = list(seed)
        for t in others:
            if t and t not in out:
                out.append(t)
        return out[:10]

    @staticmethod
    def _timestamp_bounds_from_rows(rows: list[dict[str, Any]]) -> tuple[int, int]:
        points: list[int] = []
        for row in rows:
            ts = CorrelationEnricher._to_epoch_ms(row.get("timestamp"))
            if ts:
                points.append(ts)
        if not points:
            return 0, 0
        return min(points), max(points)

    @staticmethod
    def _to_epoch_ms(value: Any) -> int:
        if isinstance(value, (int, float)):
            v = int(value)
            return v if v > 10_000_000_000 else v * 1000
        if isinstance(value, str) and value.strip():
            s = value.strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except Exception:
                return 0
        return 0

    @staticmethod
    def _signal_window(signals: list[Signal]) -> tuple[int, int]:
        if not signals:
            return 0, 0
        vals = [int(s.timestamp_ms) for s in signals]
        return min(vals), max(vals)

    @staticmethod
    def _error_window(signals: list[Signal]) -> tuple[int, int]:
        error_ts = [
            int(s.timestamp_ms)
            for s in signals
            if s.detector in {"console_error", "network_5xx", "network_4xx", "error_toast"}
        ]
        if not error_ts:
            return 0, 0
        return min(error_ts), max(error_ts)

    @staticmethod
    def _expanded_window(first_ms: int, last_ms: int) -> tuple[int, int]:
        if first_ms <= 0 and last_ms <= 0:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            return now_ms - int(timedelta(hours=24).total_seconds() * 1000), now_ms
        first = first_ms or last_ms
        last = last_ms or first_ms
        pad = int(timedelta(minutes=5).total_seconds() * 1000)
        return max(0, first - pad), max(first + 1, last + pad)

    def _error_tracking_url(
        self, *, session_id: str, distinct_id: str, issue_ids: list[str]
    ) -> str:
        params: dict[str, str] = {"session_id": session_id}
        if distinct_id:
            params["distinct_id"] = distinct_id
        if issue_ids:
            params["issue"] = issue_ids[0]
        return self._project_url("error_tracking", params)

    def _logs_url(self, *, session_id: str, distinct_id: str, trace_ids: list[str]) -> str:
        params: dict[str, str] = {"session_id": session_id}
        if distinct_id:
            params["distinct_id"] = distinct_id
        if trace_ids:
            params["trace_id"] = trace_ids[0]
        return self._project_url("logs", params)

    def _project_url(self, path: str, params: dict[str, str]) -> str:
        app_host = (self.cfg.posthog.app_host or self.cfg.posthog.host).rstrip("/")
        base = f"{app_host}/project/{self.cfg.posthog.project_id}/{path}"
        return f"{base}?{urlencode(params)}"

    @staticmethod
    def _sql_quote(value: str) -> str:
        # Small helper for controlled identifiers coming from PostHog/session metadata.
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
