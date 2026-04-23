from __future__ import annotations

from collections import defaultdict
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


def _fingerprint(signals: list[Signal]) -> tuple[str, str, str]:
    detectors = tuple(sorted({s.detector for s in signals}))
    urls = tuple(sorted({_normalize_url(s.url) for s in signals if s.url}))
    primary_url = urls[0] if urls else ""
    return (
        ",".join(detectors),
        primary_url,
        _primary_message(signals),
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