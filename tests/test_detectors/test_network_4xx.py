from tests.fixtures.events import meta, network_event


def test_network_4xx_detects_client_errors():
    from retrace.detectors.network_4xx import detector

    events = [
        meta(ts=0, href="https://app/x"),
        network_event(ts=100, url="https://api/y", status=200),
        network_event(ts=200, url="https://api/y", status=404),
        network_event(ts=300, url="https://api/y", status=500),
    ]
    signals = detector.detect("s", events)
    assert len(signals) == 1
    assert signals[0].details["status"] == 404


def test_network_4xx_ignores_401_auth_noise():
    from retrace.detectors.network_4xx import detector

    events = [
        meta(ts=0),
        network_event(ts=100, url="https://api/x", status=401),
    ]
    assert detector.detect("s", events) == []
