from tests.fixtures.events import meta, network_event


def test_network_5xx_detects_server_errors():
    from retrace.detectors.network_5xx import detector

    events = [
        meta(ts=0, href="https://app.example.com/orders"),
        network_event(ts=500, url="https://api.example.com/orders", status=200),
        network_event(
            ts=1000, url="https://api.example.com/orders", status=503, method="POST"
        ),
    ]
    signals = detector.detect("sess-1", events)
    assert len(signals) == 1
    s = signals[0]
    assert s.detector == "network_5xx"
    assert s.timestamp_ms == 1000
    assert s.url == "https://app.example.com/orders"
    assert s.details["status"] == 503
    assert s.details["request_url"].endswith("/orders")
    assert s.details["method"] == "POST"


def test_network_5xx_ignores_4xx_and_2xx():
    from retrace.detectors.network_5xx import detector

    events = [
        meta(ts=0),
        network_event(ts=100, url="https://api.example.com/x", status=404),
        network_event(ts=200, url="https://api.example.com/y", status=200),
    ]
    assert detector.detect("sess-1", events) == []