from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


_SEVERITY_RE = re.compile(r"^##\s+.*\b(Critical|High|Medium|Low)\b", re.IGNORECASE)
_TITLE_RE = re.compile(r"^###\s+(.+?)\s*$")
_SESSION_RE = re.compile(r"^- \*\*Sample session:\*\* \[[^\]]+\]\(([^)]+)\)\s*$")
_CATEGORY_RE = re.compile(r"^- \*\*Category:\*\*\s+(.+?)\s*$")


@dataclass
class ParsedFinding:
    title: str
    severity: str
    category: str
    session_url: str
    evidence_text: str

    @property
    def session_id(self) -> str:
        return self.session_url.rstrip("/").split("/")[-1]

    def finding_hash(self) -> str:
        seed = "|".join(
            [
                self.title.strip().lower(),
                self.severity.strip().lower(),
                self.category.strip().lower(),
                self.session_id.strip().lower(),
            ]
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_report_findings(path: Path) -> list[ParsedFinding]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    out: list[ParsedFinding] = []
    current_severity = "medium"
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        m_sev = _SEVERITY_RE.match(line)
        if m_sev:
            current_severity = m_sev.group(1).strip().lower()
            i += 1
            continue

        m_title = _TITLE_RE.match(line)
        if not m_title:
            i += 1
            continue

        title = m_title.group(1).strip()
        i += 1
        block: list[str] = []
        while i < n and not _TITLE_RE.match(lines[i]) and not _SEVERITY_RE.match(lines[i]):
            block.append(lines[i])
            i += 1

        session_url = ""
        category = "functional_error"
        for b in block:
            m_session = _SESSION_RE.match(b.strip())
            if m_session:
                session_url = m_session.group(1).strip()
                continue
            m_category = _CATEGORY_RE.match(b.strip())
            if m_category:
                category = m_category.group(1).strip()

        evidence_text = "\n".join(block).strip()
        if not session_url:
            continue
        out.append(
            ParsedFinding(
                title=title,
                severity=current_severity,
                category=category,
                session_url=session_url,
                evidence_text=evidence_text,
            )
        )

    return out
