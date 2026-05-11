"""Flask integration.

Hooks Flask's `got_request_exception` signal so unhandled exceptions
are captured automatically, plus a `before_request` that opens a fresh
scope per request.

Usage:

    import retrace_sdk

    retrace_sdk.init(
        dsn="...",
        integrations=[retrace_sdk.FlaskIntegration()],
    )

    app = Flask(__name__)
    retrace_sdk.FlaskIntegration.attach(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..client import get_client
from ..scope import Scope
from ._base import Integration


if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class FlaskIntegration(Integration):
    identifier = "flask"

    def setup(self, client: "Client") -> None:
        # Per-app attach happens via `attach(app)`. We could monkey-patch
        # `Flask.__init__` to auto-attach every future app, but that's
        # global mutation; we prefer the explicit one-liner.
        pass

    @staticmethod
    def attach(app: Any) -> None:
        try:
            from flask.signals import got_request_exception
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "FlaskIntegration.attach requires `flask` to be installed."
            ) from exc

        marker = "_retrace_sdk_attached"
        if getattr(app, marker, False):
            return
        setattr(app, marker, True)

        @app.before_request
        def _retrace_before_request() -> None:  # pragma: no cover - exercised via flask test client
            from flask import request

            scope = Scope.current()
            scope.clear_breadcrumbs()
            scope.set_transaction(f"{request.method} {request.path}")
            scope.add_breadcrumb(
                category="http",
                message=f"{request.method} {request.path}",
                level="info",
                data={"method": request.method, "path": request.path},
            )

        def _retrace_capture(sender, exception, **_kwargs):
            client = get_client()
            if client is None:
                return
            client.capture_exception(
                exception,
                tags={"flask.app": getattr(sender, "name", "")},
            )

        got_request_exception.connect(_retrace_capture, sender=app, weak=False)
