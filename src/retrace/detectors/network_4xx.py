from __future__ import annotations

from retrace.detectors.base import NetworkDetectorBase, register


_IGNORED_STATUSES = frozenset({401})


class Network4xxDetector(NetworkDetectorBase):
    def __init__(self) -> None:
        super().__init__(
            name="network_4xx",
            status_min=400,
            status_max=500,
            reason_code="network_4xx.status_4xx",
            confidence="medium",
            ignored_statuses=_IGNORED_STATUSES,
        )


detector = register(Network4xxDetector())
