import pytest

pytest.skip(
    "enabled after console_error detector lands in Task 5",
    allow_module_level=True,
)

from retrace.detectors import all_detectors, get_detector


def test_registry_lists_enabled_detectors():
    import retrace.detectors.console_error  # noqa: F401
    names = [d.name for d in all_detectors()]
    assert "console_error" in names


def test_get_detector_returns_by_name():
    import retrace.detectors.console_error  # noqa: F401
    d = get_detector("console_error")
    assert d is not None
    assert d.name == "console_error"
