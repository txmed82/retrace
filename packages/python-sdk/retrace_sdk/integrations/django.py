"""Django integration.

Adds a middleware class users can drop into `MIDDLEWARE`. On unhandled
exceptions it calls `client.capture_exception()` then re-raises so
Django's own 500 handler runs as usual.

Usage:

    # settings.py
    MIDDLEWARE = [
        "retrace_sdk.integrations.django.RetraceMiddleware",
        # ...
    ]

    # apps.py or wsgi.py or asgi.py
    import retrace_sdk
    retrace_sdk.init(dsn="...", integrations=[retrace_sdk.DjangoIntegration()])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from ..client import get_client
from ..scope import push_scope
from ._base import Integration


if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class DjangoIntegration(Integration):
    identifier = "django"

    def setup(self, client: "Client") -> None:
        # Django's middleware list is set per-project; users add
        # `RetraceMiddleware` themselves. No global state to touch here.
        pass


class RetraceMiddleware:
    """Django middleware (new-style — callable class)."""

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        client = get_client()
        if client is None:
            return self.get_response(request)

        method = getattr(request, "method", "GET")
        path = getattr(request, "path", "/")

        with push_scope() as sdk_scope:
            sdk_scope.set_transaction(f"{method} {path}")
            sdk_scope.add_breadcrumb(
                category="http",
                message=f"{method} {path}",
                level="info",
                data={"method": method, "path": path},
            )
            try:
                return self.get_response(request)
            except Exception:
                client.capture_exception(
                    tags={"http.method": method, "http.route": path},
                )
                raise

    def process_exception(self, request: Any, exception: BaseException) -> Optional[Any]:
        """Django's hook for view-level exceptions caught by other middlewares.

        Returning `None` tells Django to keep using its normal flow.
        We've already captured the exception in `__call__` if it
        propagated to us — this is the fallback for cases where a
        downstream middleware caught and rethrew or substituted.
        """
        client = get_client()
        if client is None:
            return None
        method = getattr(request, "method", "GET")
        path = getattr(request, "path", "/")
        client.capture_exception(
            exception,
            tags={"http.method": method, "http.route": path},
        )
        return None
