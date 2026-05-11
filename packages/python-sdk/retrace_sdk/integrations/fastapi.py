"""FastAPI / Starlette integration.

Installs an ASGI middleware that:

  - Wraps each request in a fresh `Scope` so breadcrumbs/tags don't
    leak between requests.
  - Sets the transaction name to the matched route path.
  - Adds a `request` breadcrumb with method + path.
  - On unhandled exceptions: calls `client.capture_exception()` then
    re-raises so FastAPI's own error handling still runs.

Usage:

    import retrace_sdk

    retrace_sdk.init(
        dsn="...",
        integrations=[retrace_sdk.FastAPIIntegration()],
    )

    app = FastAPI()
    retrace_sdk.FastAPIIntegration.attach(app)   # one-line wire-up
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..client import get_client
from ..scope import push_scope
from ._base import Integration


if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class FastAPIIntegration(Integration):
    identifier = "fastapi"

    def setup(self, client: "Client") -> None:
        # Nothing to do at setup time — we mount the middleware on the
        # specific app via `attach()`. (FastAPI doesn't have a global
        # `before_request` hook the way Flask does.)
        pass

    @staticmethod
    def attach(app: Any) -> None:
        """Mount the capturing middleware onto a FastAPI/Starlette app.

        Safe to call multiple times — duplicate middlewares for the same
        class are filtered by Starlette via the `user_middleware` list,
        but we also de-dupe ourselves.
        """
        # Defer the framework import until attach-time so a user without
        # FastAPI installed can still `import retrace_sdk`. We don't
        # need a starlette symbol — `add_middleware` lives on the app
        # itself — but we want a fail-loud error when the user calls
        # `.attach()` without starlette installed at all.
        try:
            import starlette  # noqa: F401  (availability check only)
        except ImportError as exc:  # pragma: no cover - covered indirectly
            raise RuntimeError(
                "FastAPIIntegration.attach requires `starlette` to be installed."
            ) from exc

        existing = getattr(app, "user_middleware", None) or []
        for m in existing:
            if getattr(m, "cls", None) is _RetraceASGIMiddleware:
                return  # already mounted

        app.add_middleware(_RetraceASGIMiddleware)


class _RetraceASGIMiddleware:
    """Pure ASGI middleware — works with FastAPI, Starlette, and any
    ASGI app. Avoids `BaseHTTPMiddleware` because it eats exceptions
    in some Starlette versions."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope_dict: dict, receive, send) -> None:
        if scope_dict.get("type") != "http":
            await self.app(scope_dict, receive, send)
            return

        client = get_client()
        if client is None:
            await self.app(scope_dict, receive, send)
            return

        with push_scope() as sdk_scope:
            method = str(scope_dict.get("method") or "GET")
            raw_path = scope_dict.get("path") or "/"
            sdk_scope.set_transaction(f"{method} {raw_path}")
            sdk_scope.add_breadcrumb(
                category="http",
                message=f"{method} {raw_path}",
                level="info",
                data={"method": method, "path": raw_path},
            )
            try:
                await self.app(scope_dict, receive, send)
            except Exception:
                client.capture_exception(
                    tags={"http.method": method, "http.route": raw_path},
                )
                raise
