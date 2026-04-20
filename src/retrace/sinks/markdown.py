from __future__ import annotations

from pathlib import Path

from retrace.sinks.base import Finding, RunSummary, Sink


_SEVERITY_ORDER = ["critical", "high", "medium", "low"]
_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}


def _render_finding(f: Finding) -> str:
    steps = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(f.reproduction_steps))
    signals = ", ".join(f.detector_signals) if f.detector_signals else "—"
    return (
        f"### {f.title}\n\n"
        f"- **Session:** [{f.session_id}]({f.session_url})\n"
        f"- **Category:** {f.category}\n"
        f"- **Confidence:** {f.confidence}\n"
        f"- **Signals:** {signals}\n\n"
        f"**What happened:** {f.what_happened}\n\n"
        f"**Likely cause:** {f.likely_cause}\n\n"
        f"**Reproduction:**\n{steps}\n"
    )


class MarkdownSink(Sink):
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, summary: RunSummary, findings: list[Finding]) -> None:
        name = summary.started_at.strftime("%Y-%m-%d-%H%M") + ".md"
        path = self.output_dir / name

        by_sev: dict[str, list[Finding]] = {sev: [] for sev in _SEVERITY_ORDER}
        for f in findings:
            by_sev.setdefault(f.severity, []).append(f)

        out: list[str] = []
        out.append(f"# Retrace report — {summary.started_at.strftime('%Y-%m-%d %H:%M')}\n")
        out.append(
            f"Scanned {summary.sessions_scanned} sessions.  "
            f"Flagged {summary.sessions_flagged}.\n"
        )

        for sev in _SEVERITY_ORDER:
            items = by_sev.get(sev, [])
            if not items:
                continue
            emoji = _SEVERITY_EMOJI.get(sev, "")
            out.append(f"## {emoji} {sev.capitalize()}\n")
            for f in items:
                out.append(_render_finding(f))

        path.write_text("\n".join(out))
