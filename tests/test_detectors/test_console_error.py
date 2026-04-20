from tests.fixtures.events import console_event, meta


def test_console_error_detects_error_level():
    from retrace.detectors.console_error import detector

    events = [
        meta(ts=1000, href="https://example.com/page"),
        console_event(ts=1500, level="log", message="ok"),
        console_event(ts=2000, level="error", message="TypeError: x is undefined"),
    ]
    signals = detector.detect("sess-1", events)
    assert len(signals) == 1
    s = signals[0]
    assert s.detector == "console_error"
    assert s.session_id == "sess-1"
    assert s.timestamp_ms == 2000
    assert s.url == "https://example.com/page"
    assert "TypeError" in s.details["message"]


def test_console_error_ignores_non_error_levels():
    from retrace.detectors.console_error import detector

    events = [
        meta(ts=0),
        console_event(ts=100, level="warn", message="hmm"),
        console_event(ts=200, level="info", message="hi"),
    ]
    assert detector.detect("sess-1", events) == []
