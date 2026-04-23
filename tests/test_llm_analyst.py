import logging
from unittest.mock import MagicMock

from retrace.detectors.base import Signal
from retrace.llm.analyst import build_prompt, analyze_session


def test_build_prompt_includes_signals_and_action_trail():
    signals = [
        Signal(
            session_id="s1",
            detector="console_error",
            timestamp_ms=1000,
            url="https://x/checkout",
            details={"message": "TypeError: y is undefined", "level": "error"},
        )
    ]
    events = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}},
        {"type": 3, "timestamp": 500, "data": {"source": 2, "type": 2, "id": 7}},
    ]
    sys, usr = build_prompt("s1", events, signals)
    assert "QA analyst" in sys.lower() or "analyst" in sys.lower()
    assert "TypeError" in usr
    assert "console_error" in usr
    assert "https://x/checkout" in usr


def test_analyze_session_returns_finding_from_llm_json():
    signals = [
        Signal(
            session_id="s1",
            detector="console_error",
            timestamp_ms=1000,
            url="https://x/checkout",
            details={"message": "boom", "level": "error"},
        )
    ]
    llm = MagicMock()
    llm.chat_json.return_value = {
        "title": "Checkout crashes on empty cart",
        "severity": "high",
        "category": "functional_error",
        "what_happened": "User hit checkout and saw nothing.",
        "likely_cause": "Null reference in render.",
        "reproduction_steps": ["open /checkout", "observe blank page"],
        "confidence": "high",
    }
    finding = analyze_session(
        llm_client=llm,
        session_id="s1",
        session_url="https://posthog/replay/s1",
        events=[{"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}}],
        signals=signals,
    )
    assert finding.title == "Checkout crashes on empty cart"
    assert finding.severity == "high"
    assert finding.session_url == "https://posthog/replay/s1"
    assert finding.detector_signals == ["console_error"]


def test_build_prompt_windows_actions_around_signal():
    # 60 events; signal at ts=5000 should bring the 5000-area actions into view
    events = [{"type": 4, "timestamp": 0, "data": {"href": "https://x/start"}}]
    events += [
        {"type": 3, "timestamp": i * 100, "data": {"source": 2, "type": 2, "id": i}}
        for i in range(1, 60)
    ]
    signals = [
        Signal(
            session_id="s",
            detector="d",
            timestamp_ms=5000,
            url="https://x/start",
            details={},
        )
    ]
    _, usr = build_prompt("s", events, signals)
    # Click near the pivot (id~50) should be present; earliest click (id=1) should NOT be
    assert "click: id=50" in usr
    assert "click: id=1\n" not in usr and "click: id=1 " not in usr


def test_analyze_session_coerces_non_list_reproduction_steps():
    from unittest.mock import MagicMock

    llm = MagicMock()
    llm.chat_json.return_value = {
        "title": "t",
        "severity": "low",
        "category": "confusion",
        "what_happened": "w",
        "likely_cause": "c",
        "reproduction_steps": "one, two",  # wrong type
        "confidence": "low",
    }
    f = analyze_session(
        llm_client=llm,
        session_id="s",
        session_url="u",
        events=[],
        signals=[
            Signal(session_id="s", detector="d", timestamp_ms=0, url="u", details={})
        ],
    )
    assert f.reproduction_steps == []


def test_analyze_session_passes_through_list_reproduction_steps():
    from unittest.mock import MagicMock

    llm = MagicMock()
    llm.chat_json.return_value = {
        "title": "t",
        "severity": "low",
        "category": "confusion",
        "what_happened": "w",
        "likely_cause": "c",
        "reproduction_steps": ["a", "b"],
        "confidence": "low",
    }
    f = analyze_session(
        llm_client=llm,
        session_id="s",
        session_url="u",
        events=[],
        signals=[
            Signal(session_id="s", detector="d", timestamp_ms=0, url="u", details={})
        ],
    )
    assert f.reproduction_steps == ["a", "b"]


def test_analyze_session_warns_when_critical_fields_empty(caplog):
    from unittest.mock import MagicMock

    llm = MagicMock()
    llm.chat_json.return_value = {}

    with caplog.at_level(logging.WARNING, logger="retrace.llm.analyst"):
        f = analyze_session(
            llm_client=llm,
            session_id="s",
            session_url="u",
            events=[],
            signals=[
                Signal(
                    session_id="s",
                    detector="d",
                    timestamp_ms=0,
                    url="u",
                    details={},
                )
            ],
        )

    assert f.title == "Unclassified issue"
    assert any(
        "empty critical fields" in rec.message.lower()
        or "degraded" in rec.message.lower()
        for rec in caplog.records
    )