"""Base class for integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class Integration:
    """Subclass and override `setup(self, client)`."""

    identifier: str = "base"

    def setup(self, client: "Client") -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError
