import dataclasses
import pytest

from retrace.detectors import Signal, all_detectors, get_detector, register
from tests.fixtures.events import click_event, console_event, meta, network_event


def test_signal_is_frozen():
    s = Signal(
        session_id="x", detector="d", timestamp_ms=0, url="https://x/", details={}
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.session_id = "y"  # type: ignore[misc]


def test_signal_each_instance_gets_its_own_details_dict():
    a = Signal(session_id="a", detector="d", timestamp_ms=0, url="u")
    b = Signal(session_id="b", detector="d", timestamp_ms=0, url="u")
    a.details["k"] = 1
    assert "k" not in b.details


def test_signal_normalizes_confidence_and_reason_codes_into_details():
    signal = Signal(
        session_id="a",
        detector="console_error",
        timestamp_ms=0,
        url="u",
        details={"confidence": "HIGH", "reason_codes": ["console_error.error_level"]},
    )

    assert signal.confidence == "high"
    assert signal.reason_codes == ("console_error.error_level",)
    assert signal.details["confidence"] == "high"
    assert signal.details["reason_codes"] == ["console_error.error_level"]


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


def _add_node_event(ts: int, attrs: dict, text: str) -> dict:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {
            "source": 0,
            "adds": [
                {
                    "node": {
                        "type": 2,
                        "tagName": "div",
                        "attributes": attrs,
                        "childNodes": [{"type": 3, "textContent": text}],
                    },
                }
            ],
        },
    }


def _sample_events_for_detector(name: str) -> list[dict]:
    if name == "network_5xx":
        return [meta(ts=0), network_event(ts=100, url="https://api/x", status=503)]
    if name == "network_4xx":
        return [meta(ts=0), network_event(ts=100, url="https://api/x", status=404)]
    if name == "console_error":
        return [meta(ts=0), console_event(ts=100, level="error", message="boom")]
    if name == "blank_render":
        return [
            meta(ts=0, href="https://x/broken"),
            _full_snapshot(ts=0, node_count=2),
            click_event(ts=3000, x=1, y=1),
        ]
    if name == "error_toast":
        return [
            meta(ts=0),
            _add_node_event(ts=100, attrs={"role": "alert"}, text="Failed to save"),
        ]
    if name == "dead_click":
        return [meta(ts=0), click_event(ts=100, x=1, y=1, target_id=7)]
    if name == "rage_click":
        return [
            meta(ts=0),
            click_event(ts=100, x=1, y=1, target_id=7),
            click_event(ts=200, x=1, y=1, target_id=7),
            click_event(ts=300, x=1, y=1, target_id=7),
        ]
    if name == "session_abandon_on_error":
        return [
            meta(ts=0),
            console_event(ts=1000, level="error", message="boom"),
            click_event(ts=2000, x=1, y=1),
        ]
    raise AssertionError(f"missing detector sample for {name}")


def test_builtin_detectors_emit_confidence_and_reason_codes():
    expected = {
        "network_5xx",
        "network_4xx",
        "console_error",
        "blank_render",
        "error_toast",
        "dead_click",
        "rage_click",
        "session_abandon_on_error",
    }
    detectors = {detector.name: detector for detector in all_detectors()}
    assert expected <= set(detectors)
    for name in sorted(expected):
        detector = detectors[name]
        signals = detector.detect("sess", _sample_events_for_detector(detector.name))
        assert signals, detector.name
        for signal in signals:
            assert signal.confidence in {"low", "medium", "high"}
            assert signal.reason_codes
            assert signal.details["confidence"] == signal.confidence
            assert signal.details["reason_codes"] == list(signal.reason_codes)


def test_register_and_get_detector_roundtrip():
    class FakeDetector:
        name = "fake_for_test"

        def detect(self, session_id, events):
            return []

    d = register(FakeDetector())
    assert get_detector("fake_for_test") is d


def test_register_raises_on_duplicate_name():
    class A:
        name = "dup_for_test"

        def detect(self, session_id, events):
            return []

    class B:
        name = "dup_for_test"

        def detect(self, session_id, events):
            return []

    register(A())
    with pytest.raises(ValueError):
        register(B())
