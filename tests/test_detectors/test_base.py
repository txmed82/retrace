import dataclasses
import pytest

from retrace.detectors import Signal, register, get_detector


def test_signal_is_frozen():
    s = Signal(session_id="x", detector="d", timestamp_ms=0, url="https://x/", details={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.session_id = "y"  # type: ignore[misc]


def test_signal_each_instance_gets_its_own_details_dict():
    a = Signal(session_id="a", detector="d", timestamp_ms=0, url="u")
    b = Signal(session_id="b", detector="d", timestamp_ms=0, url="u")
    a.details["k"] = 1
    assert "k" not in b.details


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
