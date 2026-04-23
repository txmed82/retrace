from tests.fixtures.events import meta


def _add_node_event(ts: int, tag: str, attrs: dict, text: str = "") -> dict:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {
            "source": 0,
            "adds": [
                {
                    "parentId": 1,
                    "nextId": None,
                    "node": {
                        "type": 2,
                        "tagName": tag,
                        "attributes": attrs,
                        "childNodes": (
                            [{"type": 3, "textContent": text}] if text else []
                        ),
                    },
                }
            ],
        },
    }


def test_error_toast_detects_role_alert():
    from retrace.detectors.error_toast import detector

    events = [
        meta(ts=0, href="https://x/"),
        _add_node_event(
            ts=1000, tag="div", attrs={"role": "alert"}, text="Something went wrong"
        ),
    ]
    signals = detector.detect("s", events)
    assert len(signals) == 1
    assert "Something went wrong" in signals[0].details["text"]


def test_error_toast_detects_toast_class():
    from retrace.detectors.error_toast import detector

    events = [
        meta(ts=0),
        _add_node_event(
            ts=100, tag="div", attrs={"class": "toast error"}, text="Failed to save"
        ),
    ]
    assert len(detector.detect("s", events)) == 1


def test_error_toast_ignores_unrelated_div():
    from retrace.detectors.error_toast import detector

    events = [
        meta(ts=0),
        _add_node_event(ts=100, tag="div", attrs={"class": "content"}, text="hello"),
    ]
    assert detector.detect("s", events) == []