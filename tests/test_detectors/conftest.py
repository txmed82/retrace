import pytest

from retrace.detectors import base


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(base._REGISTRY)
    try:
        yield
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(saved)
