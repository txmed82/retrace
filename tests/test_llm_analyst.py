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
