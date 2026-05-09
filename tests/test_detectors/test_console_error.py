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


def test_console_error_ignores_non_console_plugin_at_error_level():
    from retrace.detectors.console_error import detector

    events = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/"}},
        {
            "type": 6,
            "timestamp": 100,
            "data": {
                "plugin": "rrweb/network@1",  # NOT console
                "payload": {"level": "error", "payload": ["boom"]},
            },
        },
    ]
    assert detector.detect("s", events) == []


def test_console_error_detects_browser_exception_with_stack_and_trace():
    from retrace.detectors.console_error import detector

    events = [
        meta(ts=1000, href="https://example.com/checkout"),
        {
            "type": 6,
            "timestamp": 1200,
            "data": {
                "plugin": "retrace/exception@1",
                "payload": {
                    "kind": "onerror",
                    "message": "Checkout failed for dev@example.com",
                    "stack": "Error: Checkout failed\n    at pay (src/pay.ts:10:2)",
                    "source": "https://example.com/app.js",
                    "line": 10,
                    "column": 2,
                    "url": "https://example.com/checkout",
                    "sessionId": "browser-session-1",
                    "trace": {"traceId": "trace-1"},
                },
            },
        },
    ]

    signals = detector.detect("sess-1", events)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.detector == "console_error"
    assert signal.confidence == "high"
    assert signal.reason_codes == ("browser_exception",)
    assert signal.details["message"] == "Checkout failed for [redacted]"
    assert "src/pay.ts:10:2" in signal.details["stack"]
    assert signal.details["line"] == 10
    assert signal.details["column"] == 2
    assert signal.details["trace"] == {"traceId": "trace-1"}
    assert signal.details["session_id"] == "browser-session-1"
