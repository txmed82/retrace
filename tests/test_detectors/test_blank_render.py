from tests.fixtures.events import meta


def _full_snapshot(ts: int, node_count: int) -> dict:
    children = [
        {"type": 2, "tagName": "div", "attributes": {}, "childNodes": []}
        for _ in range(node_count)
    ]
    return {
        "type": 2,
        "timestamp": ts,
        "data": {"node": {"type": 0, "childNodes": children}},
    }


def test_blank_render_fires_on_low_node_count_after_navigation():
    from retrace.detectors.blank_render import detector

    events = [
        meta(ts=0, href="https://x/home"),
        _full_snapshot(ts=0, node_count=5),
        meta(ts=5000, href="https://x/broken"),
        _full_snapshot(ts=5000, node_count=2),
        {"type": 3, "timestamp": 8000, "data": {"source": 2, "type": 2, "id": 1}},
    ]
    signals = detector.detect("s", events)
    assert len(signals) == 1
    assert signals[0].url == "https://x/broken"
    assert signals[0].details["node_count"] == 2


def test_blank_render_ignores_short_page_views():
    from retrace.detectors.blank_render import detector

    events = [
        meta(ts=0, href="https://x/home"),
        _full_snapshot(ts=0, node_count=3),
        meta(ts=500, href="https://x/next"),
    ]
    assert detector.detect("s", events) == []


def test_blank_render_ignores_rich_pages():
    from retrace.detectors.blank_render import detector

    events = [
        meta(ts=0, href="https://x/home"),
        _full_snapshot(ts=0, node_count=50),
    ]
    assert detector.detect("s", events) == []
