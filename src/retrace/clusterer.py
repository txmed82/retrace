from __future__ import annotations

from collections import defaultdict
import re
from urllib.parse import urlsplit, urlunsplit

from retrace.detectors.base import Signal
from retrace.sinks.base import Cluster


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _primary_message(signals: list[Signal]) -> str:
    for s in signals:
        msg = (s.details or {}).get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:200]
    return ""


def _trim(value: object, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _top_stack_frame(stack: object) -> str:
    if not isinstance(stack, str):
        return ""
    for line in stack.splitlines():
        clean = line.strip()
        if clean and not clean.lower().startswith(("error", "typeerror", "referenceerror")):
            return _trim(clean)
    return ""


def _signal_fingerprint_part(signal: Signal) -> str:
    details = signal.details or {}
    if signal.detector in {"network_4xx", "network_5xx"}:
        return "|".join(
            [
                _trim(details.get("method") or "GET", 20).upper(),
                _normalize_url(str(details.get("request_url") or "")),
                _trim(details.get("status"), 20),
            ]
        )
    if signal.detector == "console_error":
        return "|".join(
            [
                _primary_message([signal]),
                _top_stack_frame(details.get("stack")),
            ]
        )
    if signal.detector in {"rage_click", "dead_click"}:
        return "|".join(
            [
                _trim(details.get("target_test_id") or details.get("target_id"), 120),
                _trim(
                    details.get("target_label")
                    or details.get("aria_label")
                    or details.get("label"),
                    120,
                ),
            ]
        )
    return _primary_message([signal])


def _fingerprint(signals: list[Signal]) -> tuple[str, str, str]:
    detectors = tuple(sorted({s.detector for s in signals}))
    urls = tuple(sorted({_normalize_url(s.url) for s in signals if s.url}))
    primary_url = urls[0] if urls else ""
    parts = sorted({_signal_fingerprint_part(s) for s in signals if _signal_fingerprint_part(s)})
    return (
        ",".join(detectors),
        primary_url,
        "|".join(parts) or _primary_message(signals),
    )


def cluster_sessions(
    signals_by_session: dict[str, list[Signal]],
    min_size: int = 1,
) -> list[Cluster]:
    grouped: dict[tuple[str, str, str], list[tuple[str, list[Signal]]]] = defaultdict(
        list
    )
    for sid, signals in signals_by_session.items():
        if not signals:
            continue
        fp = _fingerprint(signals)
        grouped[fp].append((sid, signals))

    clusters: list[Cluster] = []
    for fp, members in grouped.items():
        if len(members) < min_size:
            continue
        session_ids = [sid for sid, _ in members]
        all_signals = [s for _, sigs in members for s in sigs]
        summary: dict[str, int] = defaultdict(int)
        for s in all_signals:
            summary[s.detector] += 1
        timestamps = [s.timestamp_ms for s in all_signals]
        clusters.append(
            Cluster(
                fingerprint="|".join(fp),
                session_ids=session_ids,
                signal_summary=dict(summary),
                primary_url=fp[1],
                first_seen_ms=min(timestamps),
                last_seen_ms=max(timestamps),
            )
        )
    clusters.sort(key=lambda c: (-c.affected_count, c.fingerprint))
    return clusters
