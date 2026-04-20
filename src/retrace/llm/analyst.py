from __future__ import annotations

import json
from typing import Any

from retrace.detectors.base import Signal
from retrace.llm.client import LLMClient
from retrace.sinks.base import Finding


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


def _summarize_actions(events: list[dict[str, Any]], limit: int = 30) -> list[str]:
    out: list[str] = []
    for e in events:
        t = e.get("type")
        d = e.get("data") or {}
        if t == 4 and "href" in d:
            out.append(f"navigate: {d['href']}")
        elif t == 3 and d.get("source") == 2 and d.get("type") == 2:
            out.append(f"click: id={d.get('id')}")
        elif t == 3 and d.get("source") == 5:
            out.append(f"input: id={d.get('id')}")
        if len(out) >= limit:
            break
    return out


def build_prompt(
    session_id: str, events: list[dict[str, Any]], signals: list[Signal]
) -> tuple[str, str]:
    signal_lines = [
        f"- [{s.detector} @ {s.timestamp_ms}ms] {s.url} :: {json.dumps(s.details)}"
        for s in signals
    ]
    actions = _summarize_actions(events)
    user = (
        f"Session: {session_id}\n\n"
        f"Signals detected by heuristics:\n" + "\n".join(signal_lines) + "\n\n"
        f"User actions leading up to and around the issue:\n"
        + "\n".join(f"  - {a}" for a in actions)
        + "\n\n"
        + _USER_SCHEMA
    )
    return SYSTEM_PROMPT, user


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
    return Finding(
        session_id=session_id,
        session_url=session_url,
        title=str(result.get("title", "Unclassified issue")),
        severity=str(result.get("severity", "medium")),
        category=str(result.get("category", "functional_error")),
        what_happened=str(result.get("what_happened", "")),
        likely_cause=str(result.get("likely_cause", "")),
        reproduction_steps=list(result.get("reproduction_steps", []) or []),
        confidence=str(result.get("confidence", "medium")),
        detector_signals=sorted({s.detector for s in signals}),
    )
