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
from ..scope import Scope, _current_scope
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

        # Per-request scope isolation. We push a fresh Scope on every
        # request and pop it on teardown so user/tags/breadcrumbs from
        # one request can't leak into another served by the same
        # worker context. (CodeRabbit Major catch on PR #128.)
        @app.before_request
        def _retrace_before_request() -> None:  # pragma: no cover - exercised via flask test client
            from flask import g, request

            g._retrace_scope_token = _current_scope.set(Scope())
            scope = Scope.current()
            scope.set_transaction(f"{request.method} {request.path}")
            scope.add_breadcrumb(
                category="http",
                message=f"{request.method} {request.path}",
                level="info",
                data={"method": request.method, "path": request.path},
            )

        @app.teardown_request
        def _retrace_teardown_request(_exc) -> None:  # pragma: no cover - exercised via flask test client
            from flask import g

            token = getattr(g, "_retrace_scope_token", None)
            if token is None:
                return
            # Clear the attribute first so any second teardown firing
            # (e.g. when the test client closes its `with` block) sees
            # `None` and short-circuits, rather than trying to reset an
            # already-used token.
            try:
                g._retrace_scope_token = None
            except Exception:  # pragma: no cover - exotic g impls
                pass
            try:
                _current_scope.reset(token)
            except (ValueError, LookupError, RuntimeError):
                # `RuntimeError("Token has already been used once")`
                # happens when Flask's test-client `with` block fires
                # teardown twice on the same ContextVar token, or when
                # the request's scope was reset on a different thread.
                # Either way: the scope is already gone, which is fine.
                pass

        def _retrace_capture(sender, exception, **_kwargs):
            client = get_client()
            if client is None:
                return
            client.capture_exception(
                exception,
                tags={"flask.app": getattr(sender, "name", "")},
            )

        got_request_exception.connect(_retrace_capture, sender=app, weak=False)
