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
    """Django middleware (new-style — callable class).

    Both `__call__`'s try/except AND `process_exception` can fire for
    a single view exception — Django calls them on separate phases of
    its unwind. We dedupe via a `_retrace_captured` attribute on the
    request so we never double-record. (CodeRabbit Major catch on
    PR #128.)
    """

    _CAPTURED_ATTR = "_retrace_sdk_captured"

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
                self._capture_once(client, request, None, method, path)
                raise

    def process_exception(self, request: Any, exception: BaseException) -> Optional[Any]:
        """Django's hook for view-level exceptions caught by other
        middlewares before they could propagate up to our `__call__`.

        Returns `None` so Django keeps using its normal exception flow.
        """
        client = get_client()
        if client is None:
            return None
        method = getattr(request, "method", "GET")
        path = getattr(request, "path", "/")
        self._capture_once(client, request, exception, method, path)
        return None

    @classmethod
    def _capture_once(
        cls,
        client,
        request: Any,
        exception: Optional[BaseException],
        method: str,
        path: str,
    ) -> None:
        """Idempotent capture for a single request — checks/sets a
        request attribute so we record exactly once per exception."""
        if getattr(request, cls._CAPTURED_ATTR, False):
            return
        try:
            setattr(request, cls._CAPTURED_ATTR, True)
        except Exception:  # pragma: no cover - exotic request types
            pass
        client.capture_exception(
            exception,
            tags={"http.method": method, "http.route": path},
        )
