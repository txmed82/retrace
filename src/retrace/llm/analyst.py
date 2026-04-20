from __future__ import annotations

import json
import logging
from typing import Any

from retrace.detectors.base import Signal
from retrace.llm.client import LLMClient
from retrace.sinks.base import Finding

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a senior QA analyst. You review user session recordings
and explain bugs in plain English. Respond with a single JSON object matching the
requested schema. Do not add commentary outside the JSON."""


_USER_SCHEMA = """Return JSON with keys:
  title (short one-line summary of the bug)
  severity (one of: critical, high, medium, low)
  category (one of: functional_error, visual_bug, performance, confusion)
  what_happened (2-4 sentence plain-English narrative)
  likely_cause (1-2 sentences — your best guess)
  reproduction_steps (array of 2-6 short strings)
  confidence (one of: high, medium, low)
"""


def _describe_event(e: dict[str, Any]) -> str | None:
    t = e.get("type")
    d = e.get("data") or {}
    if t == 4 and "href" in d:
        return f"navigate: {d['href']}"
    if t == 3 and d.get("source") == 2 and d.get("type") == 2:
        return f"click: id={d.get('id')}"
    if t == 3 and d.get("source") == 5:
        return f"input: id={d.get('id')}"
    return None


def _window_around(
    events: list[dict[str, Any]], pivot_ts: int | None, limit: int
) -> list[dict[str, Any]]:
    """Pick ~limit events around pivot_ts. If no pivot, return first limit events.

    Takes up to limit//2 events before pivot and up to limit//2 after.
    """
    if pivot_ts is None:
        return events[:limit]

    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    half = max(1, limit // 2)
    for e in events:
        ts = int(e.get("timestamp") or 0)
        if ts <= pivot_ts:
            before.append(e)
        else:
            after.append(e)
    # Keep last `half` before, first `half` after
    return before[-half:] + after[:half]


def _summarize_actions(
    events: list[dict[str, Any]], pivot_ts: int | None, limit: int = 30
) -> list[str]:
    windowed = _window_around(events, pivot_ts, limit)
    out: list[str] = []
    for e in windowed:
        desc = _describe_event(e)
        if desc:
            out.append(desc)
        if len(out) >= limit:
            break
    return out


def build_prompt(
    session_id: str, events: list[dict[str, Any]], signals: list[Signal]
) -> tuple[str, str]:
    pivot = min((s.timestamp_ms for s in signals), default=None)
    signal_lines = [
        f"- [{s.detector} @ {s.timestamp_ms}ms] {s.url} :: {json.dumps(s.details)}"
        for s in signals
    ]
    actions = _summarize_actions(events, pivot_ts=pivot)
    user = (
        f"Session: {session_id}\n\n"
        f"Signals detected by heuristics:\n" + "\n".join(signal_lines) + "\n\n"
        f"User actions around the issue timeframe:\n"
        + "\n".join(f"  - {a}" for a in actions)
        + "\n\n"
        + _USER_SCHEMA
    )
    return SYSTEM_PROMPT, user


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def analyze_session(
    *,
    llm_client: LLMClient,
    session_id: str,
    session_url: str,
    events: list[dict[str, Any]],
    signals: list[Signal],
) -> Finding:
    system, user = build_prompt(session_id, events, signals)
    result = llm_client.chat_json(system=system, user=user)

    title = str(result.get("title") or "").strip()
    what_happened = str(result.get("what_happened") or "").strip()
    if not title or not what_happened:
        log.warning(
            "LLM returned empty critical fields for session %s; findings degraded",
            session_id,
        )

    return Finding(
        session_id=session_id,
        session_url=session_url,
        title=title or "Unclassified issue",
        severity=str(result.get("severity", "medium")),
        category=str(result.get("category", "functional_error")),
        what_happened=what_happened,
        likely_cause=str(result.get("likely_cause", "")),
        reproduction_steps=_as_string_list(result.get("reproduction_steps")),
        confidence=str(result.get("confidence", "medium")),
        detector_signals=sorted({s.detector for s in signals}),
    )
