from tests.fixtures.events import click_event, meta, network_event


def _mutation_event(ts: int) -> dict:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {"source": 0, "adds": [{"parentId": 1, "nextId": None, "node": {}}]},
    }


def test_dead_click_fires_when_click_has_no_followup():
    from retrace.detectors.dead_click import detector

    events = [
        meta(ts=0, href="https://x/"),
        click_event(ts=1000, x=10, y=10, target_id=5),
    ]
    signals = detector.detect("s", events)
    assert len(signals) == 1
    assert signals[0].details["target_id"] == 5


def test_dead_click_suppressed_by_dom_mutation():
    from retrace.detectors.dead_click import detector

    events = [
        meta(ts=0),
        click_event(ts=1000, x=0, y=0, target_id=7),
        _mutation_event(ts=1100),
    ]
    assert detector.detect("s", events) == []


def test_dead_click_suppressed_by_network_request():
    from retrace.detectors.dead_click import detector

    events = [
        meta(ts=0),
        click_event(ts=1000, x=0, y=0, target_id=7),
        network_event(ts=1500, url="https://api/x", status=200),
    ]
    assert detector.detect("s", events) == []
