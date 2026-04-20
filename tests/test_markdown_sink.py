from datetime import datetime, timezone
from pathlib import Path

from retrace.sinks.base import Finding, RunSummary
from retrace.sinks.markdown import MarkdownSink


def test_markdown_sink_writes_report_grouped_by_severity(tmp_path: Path):
    sink = MarkdownSink(output_dir=tmp_path)
    summary = RunSummary(
        started_at=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 19, 14, 3, tzinfo=timezone.utc),
        sessions_scanned=47,
        sessions_flagged=2,
    )
    findings = [
        Finding(
            session_id="s1",
            session_url="https://posthog/replay/s1",
            title="Checkout crashes on empty cart",
            severity="critical",
            category="functional_error",
            what_happened="User opened /checkout and saw a blank page.",
            likely_cause="Null reference in CartSummary.",
            reproduction_steps=["Open /checkout with no items", "Observe blank page"],
            confidence="high",
            detector_signals=["console_error"],
        ),
        Finding(
            session_id="s2",
            session_url="https://posthog/replay/s2",
            title="Submit button requires triple-click",
            severity="medium",
            category="confusion",
            what_happened="User clicked submit three times before any feedback.",
            likely_cause="Button disables only after network round-trip.",
            reproduction_steps=["Fill form", "Click submit"],
            confidence="medium",
            detector_signals=["rage_click"],
        ),
    ]

    sink.write(summary, findings)

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "Scanned 47 sessions" in text
    assert "Flagged 2" in text
    crit_idx = text.index("Critical")
    med_idx = text.index("Medium")
    assert crit_idx < med_idx
    assert "Checkout crashes on empty cart" in text
    assert "https://posthog/replay/s1" in text


def test_markdown_sink_preserves_unknown_severity(tmp_path: Path):
    sink = MarkdownSink(output_dir=tmp_path)
    summary = RunSummary(
        started_at=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 19, 14, 3, tzinfo=timezone.utc),
        sessions_scanned=1,
        sessions_flagged=1,
    )
    findings = [
        Finding(
            session_id="s1",
            session_url="u",
            title="Weird severity",
            severity="catastrophic",  # not in vocabulary
            category="functional_error",
            what_happened="...",
            likely_cause="...",
            reproduction_steps=[],
            confidence="low",
            detector_signals=[],
        )
    ]
    sink.write(summary, findings)
    text = next(tmp_path.glob("*.md")).read_text()
    assert "Weird severity" in text
    assert "Other (catastrophic)" in text


def test_markdown_sink_uniquifies_filename_on_collision(tmp_path: Path):
    sink = MarkdownSink(output_dir=tmp_path)
    summary = RunSummary(
        started_at=datetime(2026, 4, 19, 14, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 19, 14, 0, 1, tzinfo=timezone.utc),
        sessions_scanned=0,
        sessions_flagged=0,
    )
    sink.write(summary, [])
    sink.write(summary, [])
    sink.write(summary, [])
    files = sorted(p.name for p in tmp_path.glob("*.md"))
    assert files == [
        "2026-04-19-140000-2.md",
        "2026-04-19-140000-3.md",
        "2026-04-19-140000.md",
    ]
