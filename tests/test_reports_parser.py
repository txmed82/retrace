from pathlib import Path

from retrace.reports.parser import parse_report_findings


def test_parse_report_findings_extracts_sections(tmp_path: Path):
    report = tmp_path / "r.md"
    report.write_text(
        """# Retrace report — 2026-04-22 16:06 UTC

Scanned 9 sessions.  2 flagged into 2 cluster(s).

## 🟠 High

### Store button dead click

- **Sample session:** [sess-1](https://us.i.posthog.com/project/172523/replay/sess-1)
- **Category:** functional_error
- **Confidence:** high

## 🟡 Medium

### Homepage dead click

- **Sample session:** [sess-2](https://us.i.posthog.com/project/172523/replay/sess-2)
- **Category:** confusion
"""
    )

    findings = parse_report_findings(report)

    assert len(findings) == 2
    assert findings[0].title == "Store button dead click"
    assert findings[0].severity == "high"
    assert findings[0].category == "functional_error"
    assert "Sample session" in findings[0].evidence_text
    assert findings[1].title == "Homepage dead click"
    assert findings[1].severity == "medium"
    assert findings[1].category == "confusion"
