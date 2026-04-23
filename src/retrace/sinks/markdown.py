from __future__ import annotations

from datetime import timezone
from pathlib import Path

from retrace.sinks.base import Finding, RunSummary, Sink


_SEVERITY_ORDER = ["critical", "high", "medium", "low"]
_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}


def _render_finding(f: Finding) -> str:
    steps = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(f.reproduction_steps))
    signals = ", ".join(f.detector_signals) if f.detector_signals else "—"
    error_issue_ids = ", ".join(f.error_issue_ids) if f.error_issue_ids else "—"
    trace_ids = ", ".join(f.trace_ids) if f.trace_ids else "—"
    lines = [
        f"### {f.title}\n",
        f"- **Sample session:** [{f.session_id}]({f.session_url})",
    ]
    if f.affected_count > 1:
        lines.append(f"- **Affected:** {f.affected_count} sessions")
    lines.extend([
        f"- **Category:** {f.category}",
        f"- **Confidence:** {f.confidence}",
        f"- **Signals:** {signals}",
        "",
        f"**What happened:** {f.what_happened}",
        "",
        f"**Likely cause:** {f.likely_cause}",
        "",
        "**Correlated evidence:**",
        f"- Distinct ID: {f.distinct_id or '—'}",
        f"- Error issues: {error_issue_ids}",
        f"- Trace IDs: {trace_ids}",
        f"- Top stack frame: {f.top_stack_frame or '—'}",
        f"- Error window (ms): {f.first_error_ts_ms} → {f.last_error_ts_ms}"
        if (f.first_error_ts_ms or f.last_error_ts_ms)
        else "- Error window (ms): —",
        f"- Error Tracking: {f.error_tracking_url or '—'}",
        f"- Logs: {f.logs_url or '—'}",
        "",
        "**Reproduction:**",
        steps,
        "",
    ])
    return "\n".join(lines)


class MarkdownSink(Sink):
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, summary: RunSummary, findings: list[Finding]) -> None:
        started_utc = summary.started_at
        if started_utc.tzinfo is not None:
            started_utc = started_utc.astimezone(timezone.utc)

        base_name = started_utc.strftime("%Y-%m-%d-%H%M%S")
        path = self._unique_path(base_name)

        by_sev: dict[str, list[Finding]] = {sev: [] for sev in _SEVERITY_ORDER}
        unknown: dict[str, list[Finding]] = {}
        for f in findings:
            if f.severity in by_sev:
                by_sev[f.severity].append(f)
            else:
                unknown.setdefault(f.severity, []).append(f)

        out: list[str] = []
        out.append(f"# Retrace report — {started_utc.strftime('%Y-%m-%d %H:%M UTC')}\n")
        summary_line = (
            f"Scanned {summary.sessions_scanned} sessions.  "
            f"{summary.sessions_with_signals} flagged into "
            f"{summary.clusters_found} cluster(s)."
        )
        if summary.sessions_errored:
            summary_line += f"  Errored {summary.sessions_errored}."
        if summary.cap_hit:
            summary_line += "  (batch cap hit — more sessions pending)"
        out.append(summary_line + "\n")

        for sev in _SEVERITY_ORDER:
            items = by_sev.get(sev, [])
            if not items:
                continue
            emoji = _SEVERITY_EMOJI.get(sev, "")
            out.append(f"## {emoji} {sev.capitalize()}\n")
            for f in items:
                out.append(_render_finding(f))

        # Render unknown severities last under an "Other" bucket so nothing is lost.
        for sev, items in unknown.items():
            out.append(f"## ❓ Other ({sev})\n")
            for f in items:
                out.append(_render_finding(f))

        path.write_text("\n".join(out))

    def _unique_path(self, base_name: str) -> Path:
        candidate = self.output_dir / f"{base_name}.md"
        if not candidate.exists():
            return candidate
        n = 2
        while True:
            candidate = self.output_dir / f"{base_name}-{n}.md"
            if not candidate.exists():
                return candidate
            n += 1
