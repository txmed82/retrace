from retrace.detectors.base import (
    Detector,
    Signal,
    all_detectors,
    get_detector,
    iter_with_url,
    register,
)

# Register built-in detectors on package import.
from retrace.detectors import console_error as _console_error  # noqa: F401
from retrace.detectors import network_4xx as _network_4xx  # noqa: F401
from retrace.detectors import network_5xx as _network_5xx  # noqa: F401
from retrace.detectors import rage_click as _rage_click  # noqa: F401
from retrace.detectors import dead_click as _dead_click  # noqa: F401
from retrace.detectors import error_toast as _error_toast  # noqa: F401
from retrace.detectors import blank_render as _blank_render  # noqa: F401
from retrace.detectors import session_abandon as _session_abandon  # noqa: F401

__all__ = [
    "Detector",
    "Signal",
    "all_detectors",
    "get_detector",
    "iter_with_url",
    "register",
]