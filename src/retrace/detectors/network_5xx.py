from __future__ import annotations

from retrace.detectors.base import NetworkDetectorBase, register


class Network5xxDetector(NetworkDetectorBase):
    def __init__(self) -> None:
        super().__init__(
            name="network_5xx",
            status_min=500,
            status_max=600,
            reason_code="network_5xx.status_5xx",
            confidence="high",
        )


detector = register(Network5xxDetector())
