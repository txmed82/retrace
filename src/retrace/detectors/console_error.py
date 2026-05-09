from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import (
    Signal,
    event_data,
    event_timestamp_ms,
    iter_with_url,
    register,
)


_ERROR_LEVELS = {"error", "assert"}
_SENSITIVE_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"\b(?:token|secret|password|api[_-]?key)=([^&\s]+)", re.IGNORECASE),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
)


@dataclass
class ConsoleErrorDetector:
    name: str = "console_error"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if e.get("type") != 6:
                continue
            data = event_data(e)
            plugin = str(data.get("plugin", ""))
            if not (
                plugin.startswith("rrweb/console")
                or plugin.startswith("retrace/console")
                or plugin.startswith("retrace/exception")
            ):
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if plugin.startswith("retrace/exception"):
                message = _redact(str(payload.get("message") or "Browser exception"))
                stack = _redact(str(payload.get("stack") or ""))
                event_url = _redact(str(payload.get("url") or url))
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=event_timestamp_ms(e),
                        url=event_url,
                        details={
                            "message": message,
                            "level": "error",
                            "exception_kind": str(payload.get("kind") or ""),
                            "stack": stack,
                            "source": _redact(str(payload.get("source") or "")),
                            "url": event_url,
                            "line": payload.get("line"),
                            "column": payload.get("column"),
                            "trace": payload.get("trace") if isinstance(payload.get("trace"), dict) else {},
                            "session_id": str(payload.get("sessionId") or session_id),
                        },
                        confidence="high" if stack else "medium",
                        reason_codes=("browser_exception",),
                    )
                )
                continue
            level = payload.get("level")
            if level not in _ERROR_LEVELS:
                continue
            msg_parts = payload.get("payload") or []
            message = _redact(" ".join(str(p) for p in msg_parts))
            out.append(
                Signal(
                    session_id=session_id,
                    detector=self.name,
                    timestamp_ms=event_timestamp_ms(e),
                    url=url,
                    details={"message": message, "level": level},
                    confidence="medium",
                    reason_codes=("console_error.error_level",),
                )
            )
        return out


detector = register(ConsoleErrorDetector())


def _redact(value: str) -> str:
    out = value
    for pattern in _SENSITIVE_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out
