from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, iter_with_url, register


_CLASS_RE = re.compile(r"\b(toast|snackbar|error|alert|notification)\b", re.IGNORECASE)
_TEXT_RE = re.compile(
    r"\b(error|failed|couldn't|unable|something went wrong|try again)\b",
    re.IGNORECASE,
)


def _gather_text(node: dict[str, Any]) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == 3:
        return str(node.get("textContent", ""))
    kids = node.get("childNodes") or []
    return " ".join(_gather_text(k) for k in kids)


def _is_error_like(node: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(node, dict) or node.get("type") != 2:
        return False, ""
    attrs = node.get("attributes") or {}
    role = str(attrs.get("role", ""))
    klass = str(attrs.get("class", ""))
    text = _gather_text(node).strip()
    if role == "alert":
        return True, text
    if _CLASS_RE.search(klass):
        return True, text
    if text and _TEXT_RE.search(text):
        return True, text
    return False, ""


@dataclass
class ErrorToastDetector:
    name: str = "error_toast"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for url, e in iter_with_url(events):
            if e.get("type") != 3:
                continue
            data = e.get("data") or {}
            if data.get("source") != 0:
                continue
            adds = data.get("adds") or []
            for add in adds:
                node = (add or {}).get("node") or {}
                matched, text = _is_error_like(node)
                if matched:
                    out.append(
                        Signal(
                            session_id=session_id,
                            detector=self.name,
                            timestamp_ms=int(e.get("timestamp") or 0),
                            url=url,
                            details={"text": text[:200]},
                        )
                    )
                    break
        return out


detector = register(ErrorToastDetector())
