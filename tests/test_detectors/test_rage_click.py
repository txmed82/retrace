from tests.fixtures.events import click_event, meta


def test_rage_click_fires_on_three_quick_clicks_same_target():
    from retrace.detectors.rage_click import detector

    events = [
        meta(ts=0, href="https://app.example.com/checkout"),
        click_event(ts=1000, x=10, y=20, target_id=7),
        click_event(ts=1200, x=10, y=20, target_id=7),
        click_event(ts=1400, x=10, y=20, target_id=7),
    ]
    signals = detector.detect("sess-1", events)
    assert len(signals) == 1
    s = signals[0]
    assert s.detector == "rage_click"
    assert s.url == "https://app.example.com/checkout"
    assert s.details["click_count"] == 3
    assert s.details["target_id"] == 7


def test_rage_click_ignores_slow_clicks():
    from retrace.detectors.rage_click import detector

    events = [
        meta(ts=0),
        click_event(ts=0, x=10, y=20),
        click_event(ts=2000, x=10, y=20),  # > 1s gap
        click_event(ts=4000, x=10, y=20),
    ]
    assert detector.detect("sess-1", events) == []


def test_rage_click_ignores_different_targets():
    from retrace.detectors.rage_click import detector

    events = [
        meta(ts=0),
        click_event(ts=100, x=10, y=20, target_id=1),
        click_event(ts=200, x=10, y=20, target_id=2),
        click_event(ts=300, x=10, y=20, target_id=3),
    ]
    assert detector.detect("sess-1", events) == []
