from tests.fixtures.events import console_event, meta


def test_session_abandon_fires_when_error_near_end():
    from retrace.detectors.session_abandon import detector

    events = [
        meta(ts=0, href="https://x/"),
        console_event(ts=10_000, level="error", message="boom"),
        {"type": 3, "timestamp": 12_000, "data": {"source": 2, "type": 2, "id": 1}},
    ]
    signals = detector.detect("s", events)
    assert len(signals) == 1


def test_session_abandon_ignores_error_long_before_end():
    from retrace.detectors.session_abandon import detector

    events = [
        meta(ts=0),
        console_event(ts=1000, level="error", message="boom"),
        *[
            {
                "type": 3,
                "timestamp": 1000 + i * 200,
                "data": {"source": 2, "type": 2, "id": 1},
            }
            for i in range(1, 100)
        ],
    ]
    assert detector.detect("s", events) == []