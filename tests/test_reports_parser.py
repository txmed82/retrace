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


def test_parse_report_findings_parses_correlated_evidence(tmp_path: Path):
    report = tmp_path / "r.md"
    report.write_text(
        """# Retrace report — 2026-04-22 16:06 UTC

## 🟠 High

### Store button dead click

- **Sample session:** [sess-1](https://us.i.posthog.com/project/172523/replay/sess-1)
- **Category:** functional_error

**Correlated evidence:**
- Distinct ID: user-1
- Error issues: issue-a, issue-b
- Trace IDs: trace-1
- Top stack frame: explode @ app.py:11:2
- Error Tracking: https://us.i.posthog.com/project/172523/error_tracking?issue=issue-a
- Logs: https://us.i.posthog.com/project/172523/logs?trace_id=trace-1

**Reproduction:**
  1. Click button
"""
    )

    findings = parse_report_findings(report)
    assert len(findings) == 1
    f = findings[0]
    assert f.distinct_id == "user-1"
    assert f.error_issue_ids == ["issue-a", "issue-b"]
    assert f.trace_ids == ["trace-1"]
    assert f.top_stack_frame == "explode @ app.py:11:2"
    assert f.error_tracking_url and "error_tracking" in f.error_tracking_url
    assert f.logs_url and "logs" in f.logs_url
